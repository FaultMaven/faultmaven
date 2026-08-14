"""OAuth 2.0 + PKCE Service Implementation.

Implements the IOAuthService contract for Dashboard-centric authentication flow.
"""

import base64
import hashlib
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from faultmaven.models.exceptions import InvalidGrantError, InvalidRequestError
from faultmaven.modules.auth.contracts import (
    IOAuthCodeRepository,
    IOAuthService,
    OAuthAuthorizationDTO,
    OAuthCodeDTO,
    OAuthTokenDTO,
)
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    account_may_hold_credentials,
    capture_state_read_at,
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

    async def validate_authorization_request(
        self,
        request: OAuthAuthorizationDTO,
        user_id: Optional[str] = None,
    ) -> None:
        """Check an authorization request against OAuth policy. Raises, or returns.

        Split out of ``create_authorization_code`` so the *consent* leg can apply
        the same policy (#1053). ``GET /auth/oauth/authorize`` used to check only
        ``response_type`` and echo ``client_id``/``redirect_uri`` back to the
        dashboard, leaving these three checks to run at mint time — after a
        consent screen had already been rendered for a request that could never
        succeed, and after the dashboard had already been handed an unvalidated
        redirect target to send the browser to on Cancel.

        Keep this the only place the policy is expressed. Re-stating it in the
        route would put the allowlist in two files that can disagree, which is
        the failure this consolidates away.

        Args:
            request: OAuth authorization request (includes PKCE challenge)
            user_id: Authenticated user, for the audit log only — this method
                makes no user-dependent decision. The consent leg has one; a
                caller that does not is not refused.

        Raises:
            InvalidRequestError: If client_id, redirect_uri, or
                code_challenge_method is not permitted.
        """
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

    async def create_authorization_code(
        self,
        user_id: str,
        request: OAuthAuthorizationDTO,
        organization_id: Optional[str] = None,
    ) -> str:
        """Generate authorization code for OAuth flow.

        Args:
            user_id: Authenticated user's ID from Dashboard session
            request: OAuth authorization request (includes PKCE challenge)
            organization_id: Organization the authorizing session is bound to
                (#872). The caller passes the org the request was scoped to by
                ``bind_request_org_context``; see the note on the stored DTO.

        Returns:
            Authorization code (short-lived, single-use, 10 minutes)

        Raises:
            InvalidRequestError: If request parameters invalid
        """
        # #831: the basis stored on the code. Captured at this method's entry;
        # the request's org/user state was bound by middleware milliseconds
        # earlier, which is the residue of capturing here rather than at
        # request start.
        state_read_at = capture_state_read_at()

        logger.info(
            "OAuth authorization code requested",
            extra={
                "user_id": user_id,
                "client_id": request.client_id,
                "redirect_uri": request.redirect_uri,
                "scope": request.scope,
            },
        )

        await self.validate_authorization_request(request, user_id=user_id)

        # Generate cryptographically secure authorization code
        code = self._generate_code()

        # Calculate expiry time
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self.settings.oauth_code_expiry_seconds
        )

        # Store authorization code with PKCE challenge.
        #
        # The organization travels with the code (#872). This endpoint runs under
        # an authenticated dashboard session, so its org has already been verified
        # and bound by ``bind_request_org_context``; the token exchange that
        # redeems this code is unauthenticated and the user row it loads carries
        # no organization at all. Without carrying it here there is nothing left
        # to mint from, and every copilot session under multi-tenant would mint an
        # empty claim and then be refused on its first API call.
        code_data = OAuthCodeDTO(
            code=code,
            user_id=user_id,
            redirect_uri=request.redirect_uri,
            code_challenge=request.code_challenge,
            expires_at=expires_at,
            used=False,
            organization_id=organization_id,
            state_read_at=state_read_at.timestamp(),
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

        # #831: before this method's reads (code row, user row).
        state_read_at = capture_state_read_at()

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

        # The claims also derive from state the AUTHORIZE leg read — the
        # organization bound to the code (#872) — up to the code's TTL earlier.
        # The code is not revocable (no iat, no watermark check), so the stamp
        # must be the older of the two legs' bases, or a revoke-all landing
        # between authorize and exchange would be survived by a pair carrying
        # the pre-revocation tenant (#831). The basis rides the code row as
        # epoch seconds (a number cannot be naive), written by
        # ``create_authorization_code`` from its own pre-read capture — stored,
        # not derived, so reconfiguring the code TTL while codes are in flight
        # cannot shift the stamp in either direction. Absent only for codes
        # written by a pre-#831 process (or the unwired Postgres repository,
        # which does not persist it); the fallback is this leg's capture,
        # bounded by the code TTL.
        if code_data.state_read_at is not None:
            try:
                state_read_at = min(
                    state_read_at,
                    datetime.fromtimestamp(code_data.state_read_at, timezone.utc),
                )
            except (TypeError, ValueError, OverflowError, OSError):
                logger.warning(
                    "OAuth token exchange: unusable state_read_at on code row; "
                    "stamping from the exchange leg only",
                    extra={"user_id": code_data.user_id, "code_prefix": code[:8]},
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

        # A deactivated account must not be able to redeem a code, exactly as
        # ``refresh_access_token`` below refuses to rotate one.
        #
        # Deactivation revokes the account's existing tokens by writing a
        # per-user watermark (#769), and since #831 tokens minted here stamp
        # ``iat`` from the code's carried basis, so a deactivation-with-revoke
        # landing after the authorize leg DOES kill the pair downstream. This
        # check still stands on its own: it is the direct refusal (a clean 401
        # here beats a 200 with a dead pair), it covers codes carrying no
        # basis (pre-#831 writers, the unwired Postgres repository), and it
        # covers any deactivation path that did not write a watermark.
        #
        # Deactivation also soft-deletes (``user_service.deactivate_user`` sets
        # ``is_active = False`` alongside ``deleted_at``), so this one check
        # covers both.
        if not account_may_hold_credentials(user):
            logger.warning(
                "OAuth token exchange failed: user inactive",
                extra={
                    "user_id": code_data.user_id,
                    "code_prefix": code[:8],
                    "error": "USER_INACTIVE",
                },
            )
            raise InvalidGrantError(
                "User account is inactive",
                error_code="USER_INACTIVE",
            )

        # Re-attach the organization captured when the code was issued (#872),
        # the same shape ``refresh_access_token`` below uses for the rotation leg
        # and ``sso_login_service.exchange`` uses for the SSO leg. The repository
        # model has no organization column, so the object returned above carries
        # either nothing or the Standalone sentinel that ``DevUser.__post_init__``
        # stamps on every user the store loads — neither of which is this user's
        # tenant. Under single-tenant the captured value is the sentinel and
        # ``resolve_organization_claim`` would restore it anyway, so this is a
        # no-op there. ``setattr`` because the repository may return its own model
        # or a ``DevUser`` dataclass.
        #
        # Both generators put ``organization_id`` in the access *and* refresh
        # payloads, so attaching it once here carries the claim through the whole
        # chain: the first access token, and every rotation after it.
        setattr(user, "organization_id", code_data.organization_id or None)

        # Claim the code — atomically, and only now.
        #
        # LAST, not first. Everything above can fail for reasons that are not the
        # holder's fault: the user store can blip (and ``DatabaseUserStore``
        # swallows the exception, so a blip arrives here as USER_NOT_FOUND). A
        # claim spent before that work burns a valid code on a transient error,
        # and the holder must restart the whole OAuth dance because the retry
        # gets CODE_ALREADY_USED. Claiming here means only the mint itself sits
        # after the point of no return.
        #
        # ATOMIC, because moving it later would otherwise widen the replay
        # window: the ``used`` check near the top of this method is a fast-path
        # courtesy, not a gate — two concurrent redemptions can both pass it.
        # ``claim_code`` returns True to exactly one caller, so the loser lands
        # here rather than minting a second token pair (RFC 6749 §4.1.2).
        #
        # The validations above deliberately stay in front of the claim: PKCE,
        # redirect_uri and expiry all reject before anything is spent, so an
        # attacker holding a stolen code but no verifier still cannot burn it.
        if not await self.code_repository.claim_code(code):
            logger.warning(
                "OAuth token exchange failed: code already redeemed",
                extra={
                    "user_id": code_data.user_id,
                    "code_prefix": code[:8],
                    "error": "CODE_ALREADY_USED",
                },
            )
            # Recorded exactly as the early replay branch records it. Losing a
            # claim IS a failed exchange, and it is the branch a concurrent
            # replay actually lands on — if only the early check reported to
            # `record_token_exchange`, the failure rate and latency histogram
            # would omit precisely the attacks and races this method now
            # detects, and the metric would look healthiest under load.
            oauth_metrics.record_token_exchange(
                grant_type="authorization_code",
                client_id="unknown",
                duration_seconds=time.time() - start_time,
                success=False,
                error_code="CODE_ALREADY_USED",
            )
            oauth_metrics.record_code_replay_attempt("unknown")
            raise InvalidGrantError(
                "Authorization code has already been used",
                error_code="CODE_ALREADY_USED",
            )

        # Generate access token and refresh token
        access_token = await self.token_generator.generate_access_token(
            user, state_read_at=state_read_at
        )
        refresh_token = await self.token_generator.generate_refresh_token(
            user, state_read_at=state_read_at
        )

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
        # #831: before the presented token's validation and the user load, as
        # POST /auth/refresh does. No leg-1 instant to merge here: the
        # presented refresh token is itself watermark-checked, so the artifact
        # this grant redeems is already revocable.
        state_read_at = capture_state_read_at()

        logger.info(
            "OAuth token refresh requested",
            extra={
                "client_id": client_id,
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

        # A deactivated account must not be able to refresh. Deactivation also
        # writes a per-user revocation watermark (#769), which invalidates the
        # refresh credential itself; this check stays as defence in depth for
        # accounts deactivated by any path that does not revoke. Without it, a
        # refresh credential on a deactivated account could keep minting access
        # tokens on a sliding window. POST /auth/refresh already enforces this.
        if not account_may_hold_credentials(user):
            logger.warning(
                "OAuth token refresh failed: user inactive",
                extra={
                    "user_id": user_id,
                    "client_id": client_id,
                    "error": "USER_INACTIVE",
                },
            )
            raise InvalidGrantError(
                "User account is inactive",
                error_code="USER_INACTIVE",
            )

        # Re-attach the validated refresh token's organization claim before
        # minting, exactly as POST /auth/refresh step 2b does (#869 M5, extended
        # to this path by #873). Under multi-tenant it is the token chain that
        # carries tenancy — the user repository's model has no organization
        # column — so without this the D10 service credential (and any client
        # that rotates through the oauth refresh grant) would mint an org-less
        # pair on its first rotation and then be refused at
        # bind_request_org_context on every request. setattr because the
        # repository may return either its own model or a DevUser dataclass
        # (whose __post_init__ stamps the Standalone sentinel); under
        # single-tenant resolve_organization_claim restores the sentinel anyway,
        # so this is a no-op there.
        setattr(user, "organization_id", payload.get("organization_id") or None)

        # Generate new access token
        new_access_token = await self.token_generator.generate_access_token(
            user, state_read_at=state_read_at
        )

        # Rotate the refresh token: the presented token is single-use. Matches
        # POST /auth/refresh, so both refresh paths carry the same contract and a
        # client can persist the rotated token unconditionally.
        #
        # Mint BEFORE revoking, as /auth/refresh does. Revoking first would mean
        # a failure to mint leaves the caller holding a revoked credential and
        # no replacement — a lockout only an operator can undo. This order costs
        # nothing: if the revoke fails the caller simply retries with a token
        # that is still valid.
        new_refresh_token = await self.token_generator.generate_refresh_token(
            user, state_read_at=state_read_at
        )
        await self.token_generator.revoke_refresh_token(refresh_token)
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
                "token_rotated": True,
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
