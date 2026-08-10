"""JWT Token Generator Service for OAuth 2.0.

This module provides JWT token generation and validation for OAuth 2.0 flows.
Implements RS256 (RSA + SHA256) for asymmetric signing and stateless validation.

Design:
- Access tokens: Short-lived (15 minutes), stateless JWT tokens
- Refresh tokens: Long-lived (7 days), tracked for revocation
- Token rotation: One-time use refresh tokens (security best practice)
- Revocation tracking: Redis for cloud, in-memory for local
"""

import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, NamedTuple, Optional

import jwt

from faultmaven.exceptions import InactiveAccountError
from faultmaven.modules.auth.domain.models.user import User

logger = logging.getLogger(__name__)


def _max_revocation_entry_ttl() -> int:
    """Ceiling, in seconds, on how long a revocation entry is held.

    Read from the schema bound on token lifetime rather than restated here, so
    the two cannot drift: if the permitted lifetime grows, the ceiling grows
    with it and an entry still outlives the token it revokes.

    Imported inside the call, not at module scope: this module is deliberately
    free of settings imports at import time (see ``resolve_organization_claim``).
    """
    from faultmaven.config.settings import MAX_TOKEN_LIFETIME_DAYS

    return MAX_TOKEN_LIFETIME_DAYS * 86400


#: Emitted for an org-less user under multi-tenant. Falsy, so
#: ``bind_request_org_context`` refuses the request instead of binding a tenant.
_NO_ORG_CLAIM = ""

#: Lifetime of a password-reset token. Declared here because this module is the
#: only thing that signs one; ``user_service`` imports it for the Redis TTL of
#: the matching single-use key, so the token and the key it is redeemed against
#: cannot expire at different times (#959).
PASSWORD_RESET_TOKEN_EXPIRY_HOURS = 1

#: ``type`` claim discriminating a reset token from access/refresh tokens.
PASSWORD_RESET_TOKEN_TYPE = "password_reset"

#: How far into the future a caller's ``state_read_at`` may sit before the
#: mint refuses it. Same-process NTP slew can step the clock back a few
#: hundred milliseconds between capture and mint; a genuinely miswired
#: argument (an expiry, a ``now + lifetime``) is minutes out. 2 seconds
#: separates the two cleanly.
STATE_READ_AT_FUTURE_TOLERANCE_SECONDS = 2


def _mint_instant(state_read_at: datetime) -> datetime:
    """Resolve the instant a token's ``iat``/``exp`` are stamped from (#831).

    ``state_read_at`` is captured by the caller BEFORE its first read of any
    state the claims derive from — the user row, the presented refresh token,
    the authorization code, the SSO login payload. Stamping ``iat`` from it
    (rather than from mint-time ``now``) is what ties the token to the state
    it was minted from: a revocation watermark written after those reads
    began is strictly later than ``state_read_at``, so a mint that straddled
    the revocation carries ``iat <= watermark`` and dies with it. An
    ``iat`` of "now" would postdate the watermark and survive it, carrying
    pre-revocation roles or a pre-change password's authentication.

    Returns ``min(state_read_at, now)``: a ``state_read_at`` marginally in
    the future (clock slew between capture and mint) degrades to ``now``,
    which is the *smaller*, more-revocable stamp in that ordering. Beyond
    ``STATE_READ_AT_FUTURE_TOLERANCE_SECONDS`` it refuses instead — a
    far-future value is a miswired caller, and minting from it would quietly
    reopen the straddle this exists to close.

    Raises:
        ValueError: ``state_read_at`` is naive (no tzinfo) or further in the
            future than the tolerance.
    """
    if state_read_at.tzinfo is None:
        raise ValueError(
            "state_read_at must be timezone-aware; capture it with "
            "datetime.now(timezone.utc) before the first auth-state read"
        )
    now = datetime.now(timezone.utc)
    ahead = (state_read_at - now).total_seconds()
    if ahead > STATE_READ_AT_FUTURE_TOLERANCE_SECONDS:
        raise ValueError(
            f"state_read_at is {ahead:.1f}s in the future. Either the caller "
            "passed a derived time (an expiry, now + lifetime) instead of a "
            "pre-read capture, or the wall clock stepped backwards by more "
            "than the tolerance mid-request (NTP step, VM resume) — the "
            "former is a code bug; the latter is transient (though a "
            "single-use flow may need to restart)"
        )
    return min(state_read_at, now)


class PasswordResetMint(NamedTuple):
    """A minted reset token, plus what the caller needs to file it.

    ``jti`` and ``subject`` are returned rather than left to be read back out of
    the token, because the only two ways to read them are decoding without
    verifying (which puts a second, unverified claim reader into the codebase)
    or verifying a token this process signed microseconds ago (a mint path
    checking its own output on every request). Neither buys anything the minter
    does not already know.

    A decoy returns one of these too, and its ``subject`` is the random uuid4 it
    was minted with — never an account id. That is what lets the caller file a
    single-use key for every outcome without the store ever holding a row that
    names a real account (#959).
    """

    token: str
    jti: str
    subject: str


class SigningKeyUnavailableError(RuntimeError):
    """The deployment's configured signing key is missing.

    Raised by the builders below rather than deferring to PyJWT, whose error for
    a ``None`` key names neither the setting nor the mode that needed it.
    """


def _password_reset_claims(
    *,
    subject: str,
    email: str,
    issuer: str,
    audience: str,
    state_read_at: datetime,
) -> Dict:
    """Build the claim set for a password-reset token — one shape, one place.

    Shared by the real mint and the enumeration decoy, and by both algorithms.
    A JWT payload is base64, not ciphertext: whoever holds the token reads
    every claim in it, so "indistinguishable" has to mean indistinguishable to
    them. That rules out a marker value in the decoy — the caller supplies the
    address, the caller gets it back, in both cases.

    ``email`` is lowercased so the two paths cannot be told apart by it either:
    account lookup is case-insensitive, so a real token would otherwise carry
    the stored spelling while a decoy carried the submitted one, and the
    difference between them would answer the question the decoy exists to
    refuse. ``.lower()`` and not ``.casefold()`` deliberately — the repository
    matches on ``func.lower()`` in SQL and ``.lower()`` in memory, and
    casefolding would diverge from both on characters like ``ß``. Nothing reads
    this claim — ``reset_password`` uses ``sub`` and ``jti`` — so normalizing
    costs nothing.

    ``sub`` and ``jti`` are the claims that genuinely differ between a real
    token and a decoy (a real ``user_id`` and a tracked jti, vs two fresh
    uuid4s), and neither can distinguish them: all four are uuid4 strings, and
    an id the holder could recognise is one they already had.

    ``iat``/``exp`` are integers (not datetimes) because ``revocation_reason``
    compares ``iat`` against a Unix-timestamp watermark.

    ``state_read_at`` is required on the decoy path too, and the caller passes
    the SAME captured instant to both: the two paths differ in how long the
    account lookup between capture and mint took, and a decoy stamped at mint
    time while a real token is stamped at capture time would let ``iat``
    answer the existence question the decoy refuses (#831).
    """
    now = _mint_instant(state_read_at)
    expire = now + timedelta(hours=PASSWORD_RESET_TOKEN_EXPIRY_HOURS)
    return {
        "sub": subject,
        "email": (email or "").strip().lower(),
        "type": PASSWORD_RESET_TOKEN_TYPE,
        "iss": issuer,
        "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "jti": str(uuid.uuid4()),
    }


def _require_password_reset_type(claims: Dict) -> Dict:
    """Refuse a verified token that was not minted as a reset token.

    Raises ``jwt.InvalidTokenError`` — the class every other rejection on this
    path already raises — so a caller has exactly one thing to catch and cannot
    accidentally treat "wrong type" as success.
    """
    token_type = claims.get("type")
    if token_type != PASSWORD_RESET_TOKEN_TYPE:
        raise jwt.InvalidTokenError(
            f"Expected a {PASSWORD_RESET_TOKEN_TYPE} token, got {token_type!r}"
        )
    return claims


def account_may_hold_credentials(user) -> bool:
    """Whether this account is allowed to hold live tokens. THE rule, one copy.

    Before this existed the rule was written six times across five modules in
    three different spellings — and two mint paths (``POST /auth/login`` and
    ``POST /auth/register``, via ``auth.py``) had no copy at all, so a
    deactivated account could log straight back in. That is the predictable end
    state of a rule with no home: each new mint path re-derives it, and one
    eventually does not.

    **Scope, stated precisely.** ``_refuse_if_deactivated`` enforces this at
    every ``IJWTTokenGenerator`` mint, which is every path that signs from a user
    object — and, since #853, every path that signs at all. ``AuthService`` used
    to carry a second, independent signing surface that took a subject id and
    never saw an account, which this predicate could not reach; it was dead and
    is gone, so the coverage claim here is now unqualified. It stays qualified in
    one direction only: a *new* mint that does not go through
    ``IJWTTokenGenerator`` would be outside it again.

    ``is_active`` alone is sufficient and complete for users.
    ``user_service.deactivate_user`` is the only writer of ``users.deleted_at``
    and it clears ``is_active`` in the same operation, so there is no state where
    a user is soft-deleted but still active. (``sso_login_service`` additionally
    tests ``deleted_at``; that stays as belt-and-braces, and it also guards
    *organizations*, which this does not cover.)

    **Absence refuses.** An earlier version permitted it, on the reasoning that
    every user type on a mint path declares ``is_active`` so absence must mean
    "not a user object". That premise was false: ``AuthenticatedUser`` — the auth
    module's own request-path type, carrying ``user_id``, ``organization_id`` and
    ``roles`` — has no such field, because it is rebuilt from JWT claims and
    genuinely does not know whether the account is still live. It is one refactor
    away from a mint call, where permit-on-absence would have signed silently.

    Adding the field to that type would be worse: it would assert liveness the
    token cannot know. So the flag must be *present and true*. The failure mode
    of refusing is a loud, immediate lockout on a path that forgot to load the
    account — which is the direction an auth gate should fail, and is detectable
    in a way a silently-minted credential is not.
    """
    return bool(getattr(user, "is_active", False))


def _refuse_if_deactivated(user, token_kind: str) -> None:
    """Chokepoint enforcement — every mint path in the process funnels here.

    Placed in the generator rather than asked of each caller for the reason in
    ``account_may_hold_credentials``: a rule that callers must remember is a rule
    that will eventually be forgotten. Callers keep their own checks so they can
    return protocol-correct errors, but correctness no longer depends on them
    having one.
    """
    if account_may_hold_credentials(user):
        return
    user_id = getattr(user, "user_id", "<unknown>")
    logger.warning(
        "Refusing to mint a token for a deactivated account",
        extra={"user_id": user_id, "token_kind": token_kind},
    )
    # The id stays in the log, not in the message. The 403 handler echoes
    # ``str(exc)`` to the client, and POST /auth/login reaches this while still
    # unauthenticated — interpolating the id would hand an anonymous caller the
    # internal UUID of an account it just guessed the credentials for.
    raise InactiveAccountError("This account is deactivated")


def resolve_organization_claim(user: User) -> str:
    """Resolve a user's ``organization_id`` claim without inventing a tenant.

    Single-tenant: an org-less user *is* the Standalone deployment's sole tenant,
    so the sentinel org is the correct claim.

    Multi-tenant: the Standalone sentinel is **not a tenant** — it identifies the
    single-tenant deployment, and migration 033 keys the global-KB write policy
    on it. So under multi it is rejected wherever it appears, whether the user
    arrived with no organization at all or carrying a sentinel some upstream
    default invented (``DevUser.__post_init__`` stamps it on every user the
    ``DatabaseUserStore`` loads, since the repository model has no org field).
    Either way the claim is left empty, so the request fails closed at
    ``bind_request_org_context`` rather than silently pooling tenants.

    Args:
        user: User the token is being minted for.

    Returns:
        The organization id to put in the claim, or ``""`` when the deployment is
        multi-tenant and the user carries no organization of its own.
    """
    # Deferred: tenancy config pulls in settings, which must not be imported at
    # auth-module import time.
    from faultmaven.providers.tenancy.factory import (
        BUILTIN_MULTI,
        requested_tenant_provider,
    )
    from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

    organization_id = getattr(user, "organization_id", None)

    if requested_tenant_provider() != BUILTIN_MULTI:
        # Single-tenant: the sentinel is the right answer for an org-less user.
        return organization_id or SingleTenantProvider.DEFAULT_ORG_ID

    if not organization_id or organization_id == SingleTenantProvider.DEFAULT_ORG_ID:
        logger.warning(
            "Minting a token with no organization claim: user %s carries no "
            "organization under multi-tenant (%s); the request will be refused.",
            getattr(user, "user_id", "<unknown>"),
            "sentinel org" if organization_id else "no org",
        )
        return _NO_ORG_CLAIM

    return organization_id


class IJWTTokenGenerator(ABC):
    """Interface for JWT token generation and validation.

    This abstraction allows for different signing strategies (RS256, HS256)
    and different revocation backends (Redis, PostgreSQL, in-memory).
    """

    @abstractmethod
    async def generate_access_token(
        self, user: User, *, state_read_at: datetime
    ) -> str:
        """Generate short-lived access token (15 minutes).

        Args:
            user: User to generate token for
            state_read_at: Instant captured by the caller BEFORE its first
                read of any state these claims derive from (the user row, a
                presented token, an authorization code). ``iat`` and ``exp``
                are stamped from it, so a mint whose reads straddled a
                revocation carries ``iat <= watermark`` and dies with it
                (#831). Required with no default: a mint path that forgets it
                fails loudly instead of silently reopening the straddle.

        Returns:
            JWT access token string
        """
        ...

    @abstractmethod
    async def generate_refresh_token(
        self, user: User, *, state_read_at: datetime
    ) -> str:
        """Generate long-lived refresh token (7 days).

        Args:
            user: User to generate token for
            state_read_at: See ``generate_access_token`` — same contract,
                same reason (#831).

        Returns:
            JWT refresh token string
        """
        ...

    @abstractmethod
    async def generate_password_reset_token(
        self, user: User, *, state_read_at: datetime
    ) -> PasswordResetMint:
        """Generate a single-use password-reset token (1 hour).

        Signed with the same key as every other token this deployment mints —
        the reason this method exists here at all. ``UserService`` used to sign
        reset tokens itself, reaching into ``AuthService._private_key`` and
        pairing it with the configured ``jwt_algorithm``; with
        ``JWT_ALGORITHM=HS256`` that is an RSA PEM handed to an HMAC signer,
        which PyJWT refuses outright (#959). An implementation holds its own
        key and cannot make that pairing.

        Args:
            user: Account the reset is for. A deactivated one is refused at the
                same chokepoint as an access-token mint.
            state_read_at: See ``generate_access_token``. Reset tokens are
                watermark-checked at redemption (#829), so a reset mint that
                straddles a revoke-all must die with it like any other token
                (#831).

        Returns:
            The signed token and its ``jti``, so the caller can file the
            single-use key without reading claims back off the token.

        Raises:
            InactiveAccountError: The account may not hold live credentials.
        """
        ...

    @abstractmethod
    async def generate_dummy_reset_token(
        self, email: str, *, state_read_at: datetime
    ) -> PasswordResetMint:
        """Generate an enumeration decoy for a request that mints nothing.

        Same claim shape, same claim VALUES bar ``sub``/``jti``, same key and
        same algorithm as a real reset token — so "this address has no
        account", "this account is deactivated" and "here is your link" are one
        observable to whoever receives the token. That is why the requested
        address is a parameter: a payload is base64, and a decoy carrying a
        marker address announces itself to anyone who decodes it.

        (No HTTP route reaches this flow today — ``request_password_reset`` has
        no endpoint and no caller outside tests. The property is maintained
        because the flow is maintained, not because a form is live.)

        The token verifies; it simply names a subject that does not exist, and
        is refused at redemption like any other unusable link.

        Returns a mint, not a bare token, so the caller can file a single-use
        key for a decoy exactly as it does for a real token. The alternative —
        writing to the store only on the real branch — makes a store outage an
        existence oracle, which is the enumeration the decoy exists to prevent.

        Args:
            email: The address the caller asked about, echoed into the claims.
            state_read_at: The SAME instant the caller would pass to the real
                mint — captured before the account lookup, on both branches.
                A decoy stamped at mint time while a real token is stamped at
                capture time would let ``iat`` carry the lookup's latency and
                answer the existence question (#831).

        Returns:
            The signed token, its ``jti``, and the random subject it names.
        """
        ...

    @abstractmethod
    async def verify_password_reset_token(self, token: str) -> Dict:
        """Verify a password-reset token's signature, issuer, audience and type.

        Revocation and single-use consumption are deliberately NOT checked here:
        both are storage-coupled policy owned by ``UserService`` (the shared
        revocation store and the one-time Redis key). This method answers only
        "did this deployment sign this, as a reset token, and is it still
        within its hour".

        Args:
            token: JWT reset token

        Returns:
            The verified claims.

        Raises:
            jwt.InvalidTokenError: Bad signature, wrong issuer/audience,
                expired, or not a ``password_reset`` token.
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

        Implementations record nothing for a token this deployment did not
        sign: the OAuth revoke endpoint is unauthenticated (RFC 7009), so the
        signature is what stands between a caller and the store (#830).

        Args:
            token: JWT access token to revoke
        """
        ...

    @abstractmethod
    async def revoke_refresh_token(self, token: str) -> None:
        """Revoke refresh token (prevent future use).

        Implementations record nothing for a token this deployment did not
        sign — see ``revoke_access_token``.

        Args:
            token: JWT refresh token to revoke
        """
        ...


class RS256JWTTokenGenerator(IJWTTokenGenerator):
    """JWT token generator using RS256 (RSA + SHA256).

    Uses asymmetric signing for stateless token validation:
    - Private key for signing (server-side only)
    - Public key for validation (any service can validate without the private key)

    Token Structure (identical to HS256):
    - Access Token: {sub, username, email, roles, scopes, organization_id,
                     exp, iat, iss, aud, jti, type: "access", auth_mode: "oauth"}
    - Refresh Token: {sub, exp, iat, iss, aud, jti, type: "refresh"}
    """

    def __init__(
        self,
        private_key: str,
        public_key: str,
        revocation_store,  # ITokenRevocationStore
        access_token_expire_minutes: int,
        refresh_token_expire_days: int,
        issuer: str,
        audience: str,
    ):
        """Initialize JWT token generator.

        Lifetimes are explicit parameters rather than a settings object so that
        which settings half the caller holds can never decide a token's
        lifetime (#888). The same holds for ``issuer``/``audience`` (#938).
        None of the four has a local default to fall back to, so a construction
        site that forgets to wire the configured value fails at construction
        instead of minting a plausible-looking one.

        Args:
            private_key: RSA private key (PEM format) for signing
            public_key: RSA public key (PEM format) for validation
            revocation_store: Token revocation tracking storage
            access_token_expire_minutes: Access token lifetime, in minutes
                (``JWT_ACCESS_TOKEN_EXPIRY_MINUTES``)
            refresh_token_expire_days: Refresh token lifetime, in DAYS
                (``JWT_REFRESH_TOKEN_EXPIRY_DAYS``)
            issuer: JWT issuer (iss claim) — ``JWT_ISSUER``
            audience: JWT audience (aud claim) — ``JWT_AUDIENCE``
        """
        self.private_key = private_key
        self.public_key = public_key
        self.revocation_store = revocation_store
        self.access_token_expire_minutes = access_token_expire_minutes
        self.refresh_token_expire_days = refresh_token_expire_days
        self.issuer = issuer
        self.audience = audience

    async def generate_access_token(
        self, user: User, *, state_read_at: datetime
    ) -> str:
        """Generate RS256-signed access token.

        Token Claims (per iam-design.md):
        - sub: user_id (subject)
        - username: user's username
        - email: user's email
        - roles: user roles list
        - scopes: OAuth scopes
        - organization_id: organization the user belongs to
        - exp: expiration timestamp
        - iat: issued at timestamp
        - iss: the configured issuer (``JWT_ISSUER``)
        - aud: the configured audience (``JWT_AUDIENCE``)
        - jti: JWT ID (for revocation tracking)
        - type: "access" (token type discriminator)
        - auth_mode: "oauth" (authentication mode)

        Args:
            user: User to generate token for
            state_read_at: See interface (#831)

        Returns:
            JWT access token string
        """
        _refuse_if_deactivated(user, "access")

        now = _mint_instant(state_read_at)
        expires_at = now + timedelta(minutes=self.access_token_expire_minutes)

        jti = str(uuid.uuid4())

        organization_id = resolve_organization_claim(user)

        payload = {
            "sub": user.user_id,
            "username": user.username,
            "email": user.email if hasattr(user, "email") else "",
            "organization_id": organization_id,
            "roles": user.roles if hasattr(user, "roles") else ["user"],
            "scopes": [
                "openid",
                "profile",
                "email",
                "cases:read",
                "cases:write",
                "knowledge:read",
            ],
            "exp": expires_at,
            "iat": now,
            "iss": self.issuer,
            "aud": self.audience,
            "jti": jti,
            "type": "access",
            "auth_mode": "oauth",
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
                "expires_in_minutes": self.access_token_expire_minutes,
            },
        )
        return token

    async def generate_refresh_token(
        self, user: User, *, state_read_at: datetime
    ) -> str:
        """Generate RS256-signed refresh token.

        Token Claims:
        - sub: user_id (subject)
        - exp: expiration timestamp (7 days)
        - iat: issued at timestamp
        - jti: JWT ID (for revocation tracking)
        - type: "refresh" (token type discriminator)
        - organization_id: organization the refreshed session belongs to

        The organization claim rides the refresh token because rotation is the
        only thing that carries tenancy across an access token's lifetime: the
        user store's model has no organization column, so `/auth/refresh` would
        otherwise re-mint an org-less pair and the session would fail closed on
        its first refresh (#869).

        Args:
            user: User to generate token for
            state_read_at: See interface (#831)

        Returns:
            JWT refresh token string
        """
        _refuse_if_deactivated(user, "refresh")

        now = _mint_instant(state_read_at)
        expires_at = now + timedelta(days=self.refresh_token_expire_days)

        jti = str(uuid.uuid4())

        payload = {
            "sub": user.user_id,
            "exp": expires_at,
            "iat": now,
            "iss": self.issuer,
            "aud": self.audience,
            "jti": jti,
            "type": "refresh",
            "organization_id": resolve_organization_claim(user),
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
                "expires_in_days": self.refresh_token_expire_days,
            },
        )
        return token

    async def generate_password_reset_token(
        self, user: User, *, state_read_at: datetime
    ) -> PasswordResetMint:
        """Generate an RS256-signed password-reset token (see interface).

        Args:
            user: Account the reset is for
            state_read_at: See interface (#831)

        Returns:
            The signed token and its ``jti``
        """
        _refuse_if_deactivated(user, PASSWORD_RESET_TOKEN_TYPE)

        claims = _password_reset_claims(
            subject=user.user_id,
            email=getattr(user, "email", "") or "",
            issuer=self.issuer,
            audience=self.audience,
            state_read_at=state_read_at,
        )
        token = jwt.encode(claims, self.private_key, algorithm="RS256")

        logger.info(
            "Password reset token generated",
            extra={"user_id": user.user_id, "jti": claims["jti"]},
        )
        return PasswordResetMint(token=token, jti=claims["jti"], subject=claims["sub"])

    async def generate_dummy_reset_token(
        self, email: str, *, state_read_at: datetime
    ) -> PasswordResetMint:
        """Generate an RS256-signed enumeration decoy (see interface).

        Args:
            email: The address the caller asked about
            state_read_at: See interface (#831)

        Returns:
            The signed token, its ``jti``, and the random subject it names
        """
        claims = _password_reset_claims(
            subject=str(uuid.uuid4()),
            email=email,
            issuer=self.issuer,
            audience=self.audience,
            state_read_at=state_read_at,
        )
        return PasswordResetMint(
            token=jwt.encode(claims, self.private_key, algorithm="RS256"),
            jti=claims["jti"],
            subject=claims["sub"],
        )

    async def verify_password_reset_token(self, token: str) -> Dict:
        """Verify an RS256-signed reset token using the public key.

        Args:
            token: JWT reset token

        Returns:
            The verified claims

        Raises:
            jwt.InvalidTokenError: Token is not a valid reset token.
        """
        claims = jwt.decode(
            token,
            self.public_key,
            algorithms=["RS256"],
            issuer=self.issuer,
            audience=self.audience,
        )
        return _require_password_reset_type(claims)

    async def validate_access_token(self, token: str) -> Optional[Dict]:
        """Validate access token using public key.

        Verification:
        1. Signature verification (RS256)
        2. Expiration check
        3. Token type check (must be "access")
        4. Revocation check (jti and per-user watermark)

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
                audience=self.audience,
                issuer=self.issuer,
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

            # Check revocation status (per-token jti and per-user watermark)
            jti = payload.get("jti")
            reason = await revocation_reason(self.revocation_store, payload)
            if reason:
                logger.info(
                    "JWT validation failed: token revoked",
                    extra={
                        "jti": jti,
                        "user_id": payload.get("sub"),
                        "reason": reason,
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
        4. Revocation check (jti and per-user watermark; CRITICAL for refresh)

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
                audience=self.audience,
                issuer=self.issuer,
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

            reason = await revocation_reason(self.revocation_store, payload)
            if reason:
                logger.info(
                    "JWT validation failed: refresh token revoked",
                    extra={
                        "jti": jti,
                        "user_id": payload.get("sub"),
                        "reason": reason,
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

    def _decode_for_revocation(self, token: str) -> Dict:
        """Decode a token for revocation, verifying it was issued here (#830).

        Audience, issuer and ``type`` are deliberately NOT checked: RFC 7009's
        ``token_type_hint`` is a hint, and any token this deployment signed is
        revocable whatever it was minted for. Expiry IS checked — an expired
        token has nothing left to revoke.

        Raises:
            jwt.InvalidTokenError: The token was not signed by this deployment,
                is malformed, or has already expired.
        """
        return jwt.decode(
            token,
            self.public_key,
            algorithms=["RS256"],
            options={"verify_exp": True, "verify_aud": False},
        )

    async def revoke_access_token(self, token: str) -> None:
        """Revoke access token by adding jti to the revocation store.

        Args:
            token: JWT access token to revoke

        Raises:
            Exception: Store write failures propagate — a caller must not
                report revocation success when the token remains usable.
        """
        await _revoke_token_by_jti(
            self.revocation_store,
            token,
            token_kind="access",
            decode_verified=self._decode_for_revocation,
        )

    async def revoke_refresh_token(self, token: str) -> None:
        """Revoke refresh token by adding jti to the revocation store.

        Args:
            token: JWT refresh token to revoke

        Raises:
            Exception: Store write failures propagate — a caller must not
                report revocation success when the token remains usable.
        """
        await _revoke_token_by_jti(
            self.revocation_store,
            token,
            token_kind="refresh",
            decode_verified=self._decode_for_revocation,
        )


async def _revoke_token_by_jti(
    revocation_store,
    token: str,
    *,
    token_kind: str,
    decode_verified: Callable[[str], Dict],
) -> None:
    """Record a token's jti in the revocation store (shared by both generators).

    ``decode_verified`` must VERIFY the token's signature before returning its
    claims. ``POST /auth/oauth/revoke`` is unauthenticated by design (RFC 7009),
    so without that check any caller could write a key of their choosing — with
    a TTL of their choosing, from a crafted ``exp`` — into the revocation store
    (#830). Nothing is recorded for a token this deployment did not sign.

    The entry's ceiling is ``MAX_TOKEN_LIFETIME_DAYS`` — the longest lifetime
    ANY permitted configuration can mint — not the currently configured lifetime
    for some token type. An entry that expires before the token it revokes
    resurrects that token, and three things can produce one: a token type with
    its own lifetime (``password_reset`` is signed with the same key and
    verifies here), a token minted before an operator lowered the setting, and
    a hint that routes a refresh token onto the access path (``token_type_hint``
    is optional in RFC 7009). An absolute bound is immune to all three while
    still keeping store memory bounded, which is the only thing the cap is for.

    ``token_kind`` only labels the log line with the path taken.

    Invalid input — unsigned/forged tokens, undecodable tokens, missing jti,
    malformed/overflowing exp claims — is tolerated (logged, no-op): RFC 7009
    treats revocation of an invalid token as success, and such a token has
    nothing to revoke. Only STORE WRITE failures propagate, so revoke endpoints
    cannot report success while a real token remains usable (#767).
    """
    try:
        # The exp/TTL math shares this block with the decode: a crafted or
        # ms-precision exp (e.g. 9999999999999) overflows fromtimestamp and is
        # an invalid-token case, not a store failure.
        payload = decode_verified(token)

        jti = payload.get("jti")
        if not jti:
            logger.warning(
                "JWT revocation skipped: token missing jti",
                extra={"token_kind": token_kind, "user_id": payload.get("sub")},
            )
            return

        # Revocation entry lives exactly as long as the token could be used
        max_ttl = _max_revocation_entry_ttl()
        exp = payload.get("exp")
        if exp:
            expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
            ttl = int((expires_at - datetime.now(timezone.utc)).total_seconds())
            if ttl <= 0:
                return  # Already expired; nothing left to revoke
            # Bound the entry so a crafted exp cannot buy multi-year storage
            # (#830), without ever cutting it below the token's own life.
            ttl = min(ttl, max_ttl)
        else:
            # A signed token with no exp never expires on its own; the ceiling
            # is the longest this store will hold anything.
            ttl = max_ttl
    except jwt.ExpiredSignatureError:
        # Nothing left to revoke; the token is already unusable.
        logger.info(
            "JWT revocation skipped: token already expired",
            extra={"token_kind": token_kind},
        )
        return
    except Exception as e:
        logger.warning(
            "JWT revocation skipped: token could not be verified",
            extra={"token_kind": token_kind, "error": str(e)},
        )
        return

    await revocation_store.add_revoked_token(jti, ttl)
    logger.info(
        f"JWT {token_kind} token revoked",
        extra={
            "jti": jti,
            "user_id": payload.get("sub"),
            "ttl_seconds": ttl,
        },
    )


async def revocation_reason(revocation_store, payload: Dict) -> Optional[str]:
    """Return why a token's claims are revoked, or None if they are not.

    The single place the two revocation arms are combined, so no validate path
    can apply a different rule than another (the disagreement that #767 fixed
    for the per-token arm):

    - ``"token_revoked"`` — this specific jti was revoked.
    - ``"user_revoked"`` — the user's watermark is at or after this token's
      ``iat``, i.e. the token predates a bulk per-user revocation (#769).

    A token missing ``sub``/``iat`` cannot be matched against a watermark, so
    only the jti arm applies to it. Comparison is ``iat <= watermark`` rather
    than ``<``: ``iat`` has whole-second granularity, and honouring a token
    minted in the same second as a revocation is the more dangerous of the two
    rounding errors.
    """
    jti = payload.get("jti")
    if jti and await revocation_store.is_revoked(jti):
        return "token_revoked"

    user_id = payload.get("sub")
    issued_at = payload.get("iat")
    if user_id and issued_at is not None:
        if await revocation_store.is_user_revoked(user_id, int(issued_at)):
            return "user_revoked"

    return None


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
        access_token_expire_minutes: int,
        refresh_token_expire_days: int,
        issuer: str,
        audience: str,
    ):
        """Initialize JWT token generator.

        Lifetimes are explicit parameters rather than a settings object, and are
        the same two values the RS256 generator takes — one configured source
        for both auth modes (#888).

        ``issuer``/``audience`` are required for the same reason (#938). They
        used to default to ``"faultmaven"``/``"faultmaven-api"``, which are not
        the values ``JWT_ISSUER``/``JWT_AUDIENCE`` default to — so an omitting
        caller got a generator that disagreed with every other decoder in the
        deployment. There is no issuer or audience independent of settings;
        making them required means a caller cannot obtain the wrong pair by
        saying nothing.

        Args:
            secret_key: Secret key for HS256 signing/validation
            revocation_store: Token revocation tracking storage
            access_token_expire_minutes: Access token lifetime, in minutes
                (``JWT_ACCESS_TOKEN_EXPIRY_MINUTES``)
            refresh_token_expire_days: Refresh token lifetime, in DAYS
                (``JWT_REFRESH_TOKEN_EXPIRY_DAYS``)
            issuer: JWT issuer (iss claim) — ``JWT_ISSUER``
            audience: JWT audience (aud claim) — ``JWT_AUDIENCE``
        """
        self.secret_key = secret_key
        self.revocation_store = revocation_store
        self.access_token_expire_minutes = access_token_expire_minutes
        self.refresh_token_expire_days = refresh_token_expire_days
        self.issuer = issuer
        self.audience = audience

    async def generate_access_token(
        self, user: User, *, state_read_at: datetime
    ) -> str:
        """Generate HS256-signed access token.

        Token Claims (per iam-design.md):
        - sub: user_id (subject)
        - username: user's username
        - email: user's email
        - roles: user roles list
        - scopes: OAuth scopes (for compatibility)
        - exp: expiration timestamp
        - iat: issued at timestamp
        - iss: the configured issuer (``JWT_ISSUER``)
        - aud: the configured audience (``JWT_AUDIENCE``)
        - jti: JWT ID (for revocation tracking)
        - type: "access" (token type discriminator)
        - auth_mode: "local" (authentication mode)

        Args:
            user: User to generate token for
            state_read_at: See interface (#831)

        Returns:
            JWT access token string
        """
        _refuse_if_deactivated(user, "access")

        now = _mint_instant(state_read_at)
        expires_at = now + timedelta(minutes=self.access_token_expire_minutes)

        jti = str(uuid.uuid4())

        # Build payload matching iam-design.md spec
        organization_id = resolve_organization_claim(user)

        payload = {
            "sub": user.user_id,  # Subject (user ID)
            "username": user.username,
            "email": user.email if hasattr(user, "email") else "",
            "organization_id": organization_id,  # Organization ID (required for all modes)
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
                "expires_in_minutes": self.access_token_expire_minutes,
                "auth_mode": "local",
            },
        )
        return token

    async def generate_refresh_token(
        self, user: User, *, state_read_at: datetime
    ) -> str:
        """Generate HS256-signed refresh token.

        Token Claims:
        - sub: user_id (subject)
        - exp: expiration timestamp (7 days)
        - iat: issued at timestamp
        - iss: the configured issuer (``JWT_ISSUER``)
        - aud: the configured audience (``JWT_AUDIENCE``)
        - jti: JWT ID (for revocation tracking)
        - type: "refresh" (token type discriminator)
        - organization_id: organization the refreshed session belongs to

        Carried here for payload-shape parity with RS256 (#869): both
        algorithms mint the same refresh claims, so `/auth/refresh` has one
        re-attachment rule regardless of auth mode.

        Args:
            user: User to generate token for
            state_read_at: See interface (#831)

        Returns:
            JWT refresh token string
        """
        _refuse_if_deactivated(user, "refresh")

        now = _mint_instant(state_read_at)
        expires_at = now + timedelta(days=self.refresh_token_expire_days)

        jti = str(uuid.uuid4())

        payload = {
            "sub": user.user_id,  # Subject (user ID)
            "exp": expires_at,  # Expiration time
            "iat": now,  # Issued at
            "iss": self.issuer,  # Issuer
            "aud": self.audience,  # Audience
            "jti": jti,  # JWT ID (unique identifier)
            "type": "refresh",  # Token type
            "organization_id": resolve_organization_claim(user),
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
                "expires_in_days": self.refresh_token_expire_days,
            },
        )
        return token

    async def generate_password_reset_token(
        self, user: User, *, state_read_at: datetime
    ) -> PasswordResetMint:
        """Generate an HS256-signed password-reset token (see interface).

        Args:
            user: Account the reset is for
            state_read_at: See interface (#831)

        Returns:
            The signed token and its ``jti``
        """
        _refuse_if_deactivated(user, PASSWORD_RESET_TOKEN_TYPE)

        claims = _password_reset_claims(
            subject=user.user_id,
            email=getattr(user, "email", "") or "",
            issuer=self.issuer,
            audience=self.audience,
            state_read_at=state_read_at,
        )
        token = jwt.encode(claims, self.secret_key, algorithm="HS256")

        logger.info(
            "Password reset token generated (HS256)",
            extra={"user_id": user.user_id, "jti": claims["jti"]},
        )
        return PasswordResetMint(token=token, jti=claims["jti"], subject=claims["sub"])

    async def generate_dummy_reset_token(
        self, email: str, *, state_read_at: datetime
    ) -> PasswordResetMint:
        """Generate an HS256-signed enumeration decoy (see interface).

        Args:
            email: The address the caller asked about
            state_read_at: See interface (#831)

        Returns:
            The signed token, its ``jti``, and the random subject it names
        """
        claims = _password_reset_claims(
            subject=str(uuid.uuid4()),
            email=email,
            issuer=self.issuer,
            audience=self.audience,
            state_read_at=state_read_at,
        )
        return PasswordResetMint(
            token=jwt.encode(claims, self.secret_key, algorithm="HS256"),
            jti=claims["jti"],
            subject=claims["sub"],
        )

    async def verify_password_reset_token(self, token: str) -> Dict:
        """Verify an HS256-signed reset token using the shared secret.

        Args:
            token: JWT reset token

        Returns:
            The verified claims

        Raises:
            jwt.InvalidTokenError: Token is not a valid reset token.
        """
        claims = jwt.decode(
            token,
            self.secret_key,
            algorithms=["HS256"],
            issuer=self.issuer,
            audience=self.audience,
        )
        return _require_password_reset_type(claims)

    async def validate_access_token(self, token: str) -> Optional[Dict]:
        """Validate access token using secret key.

        Verification:
        1. Signature verification (HS256)
        2. Expiration check
        3. Issuer/Audience check
        4. Token type check (must be "access")
        5. Revocation check (jti and per-user watermark)

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
                audience=self.audience,
                issuer=self.issuer,
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

            # Check revocation status (per-token jti and per-user watermark)
            jti = payload.get("jti")
            reason = await revocation_reason(self.revocation_store, payload)
            if reason:
                logger.info(
                    "JWT validation failed: token revoked",
                    extra={
                        "jti": jti,
                        "user_id": payload.get("sub"),
                        "reason": reason,
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
                audience=self.audience,
                issuer=self.issuer,
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

            # Check revocation status (per-token jti and per-user watermark)
            jti = payload.get("jti")
            reason = await revocation_reason(self.revocation_store, payload)
            if reason:
                logger.info(
                    "JWT validation failed: refresh token revoked",
                    extra={
                        "jti": jti,
                        "user_id": payload.get("sub"),
                        "reason": reason,
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

    def _decode_for_revocation(self, token: str) -> Dict:
        """Decode a token for revocation, verifying it was issued here (#830).

        Audience, issuer and ``type`` are deliberately NOT checked: RFC 7009's
        ``token_type_hint`` is a hint, and any token this deployment signed is
        revocable whatever it was minted for. Expiry IS checked — an expired
        token has nothing left to revoke.

        Raises:
            jwt.InvalidTokenError: The token was not signed by this deployment,
                is malformed, or has already expired.
        """
        return jwt.decode(
            token,
            self.secret_key,
            algorithms=["HS256"],
            options={"verify_exp": True, "verify_aud": False},
        )

    async def revoke_access_token(self, token: str) -> None:
        """Revoke access token by adding jti to the revocation store.

        Args:
            token: JWT access token to revoke

        Raises:
            Exception: Store write failures propagate — a caller must not
                report revocation success when the token remains usable.
        """
        await _revoke_token_by_jti(
            self.revocation_store,
            token,
            token_kind="access",
            decode_verified=self._decode_for_revocation,
        )

    async def revoke_refresh_token(self, token: str) -> None:
        """Revoke refresh token by adding jti to the revocation store.

        Args:
            token: JWT refresh token to revoke

        Raises:
            Exception: Store write failures propagate — a caller must not
                report revocation success when the token remains usable.
        """
        await _revoke_token_by_jti(
            self.revocation_store,
            token,
            token_kind="refresh",
            decode_verified=self._decode_for_revocation,
        )


def _auth_mode_name(settings) -> str:
    """Return the configured auth mode as a plain lowercase name.

    ``AuthMode`` is a ``(str, Enum)``, so ``str(member)`` is ``'AuthMode.LOCAL'``
    — matching on that is how a mode check silently selects the wrong branch
    (#881). Read ``.value`` when present so a plain-string override matches too.
    """
    mode = getattr(getattr(settings, "auth", None), "auth_mode", None)
    if mode is None:
        return "local"
    return str(getattr(mode, "value", mode)).strip().lower()


def build_hs256_token_generator(settings, revocation_store) -> HS256JWTTokenGenerator:
    """Build the HS256 generator from settings (local-mode signing).

    Args:
        settings: FaultMavenSettings (or an equivalent) carrying ``auth`` and
            ``security`` sections.
        revocation_store: The deployment-wide revocation store (#767).

    Raises:
        SigningKeyUnavailableError: ``JWT_SECRET_KEY`` is unset. In local mode
            ``get_settings()`` generates and persists one, so this means the
            secret was explicitly cleared or the persist failed.
    """
    secret = getattr(settings.security, "jwt_secret_key", None)
    if not secret:
        raise SigningKeyUnavailableError(
            "JWT_SECRET_KEY not configured for local mode authentication"
        )

    return HS256JWTTokenGenerator(
        secret_key=secret.get_secret_value(),
        revocation_store=revocation_store,
        # Lifetimes come from the single expiry source (#888); the security half
        # carries only the secret, issuer and audience.
        access_token_expire_minutes=settings.auth.jwt_access_token_expire_minutes,
        refresh_token_expire_days=settings.auth.jwt_refresh_token_expire_days,
        issuer=settings.security.jwt_issuer,
        audience=settings.security.jwt_audience,
    )


def build_rs256_token_generator(
    settings,
    revocation_store,
    *,
    private_key: Optional[str],
    public_key: Optional[str],
) -> RS256JWTTokenGenerator:
    """Build the RS256 generator (cloud/OAuth signing) from a RESOLVED pair.

    The pair is a parameter, never read from settings here. ``AuthService``
    resolves keys — direct value, file path, or deliberately-selected
    development pair, refusing a half-configured one — and a second resolver in
    this module would disagree with it in exactly the cases that matter: a
    path-configured install (settings alone yields ``None``) and an
    unconfigured one (two independent dev pairs in one process, so tokens
    minted by one fail verification by the other). One resolver, passed around.

    Args:
        settings: FaultMavenSettings (or an equivalent) carrying ``auth`` and
            ``security`` sections — read for lifetimes, issuer and audience.
        revocation_store: The deployment-wide revocation store (#767).
        private_key: Resolved RSA private key (PEM).
        public_key: Resolved RSA public key (PEM).

    Raises:
        SigningKeyUnavailableError: Either half is missing. This is a caller
            error, not a configuration state: ``AuthService`` always holds a
            complete pair by the time it can be asked for one, so on the
            container path it cannot fire. It exists so a keyless generator is
            refused at construction rather than discovered by PyJWT at the
            first mint, with a raw ``TypeError``.
    """
    if not private_key or not public_key:
        missing = " and ".join(
            name
            for name, value in (("private", private_key), ("public", public_key))
            if not value
        )
        raise SigningKeyUnavailableError(
            f"RS256 generator requested with no {missing} key. Keys are "
            "resolved by AuthService and passed in; nothing else resolves them."
        )

    return RS256JWTTokenGenerator(
        private_key=private_key,
        public_key=public_key,
        revocation_store=revocation_store,
        # Lifetimes come from the single expiry source on the auth half (#888) —
        # the same values the local HS256 generator is built with, so the
        # documented knob governs cloud tokens too. Keys, issuer and audience
        # remain the security half's, which is where they are declared.
        access_token_expire_minutes=settings.auth.jwt_access_token_expire_minutes,
        refresh_token_expire_days=settings.auth.jwt_refresh_token_expire_days,
        issuer=settings.security.jwt_issuer,
        audience=settings.security.jwt_audience,
    )


def build_jwt_token_generator(
    settings,
    revocation_store,
    *,
    private_key: Optional[str] = None,
    public_key: Optional[str] = None,
) -> IJWTTokenGenerator:
    """THE answer to "which generator does this deployment sign with" (#959).

    Selection is on ``AUTH_MODE``, not ``JWT_ALGORITHM``: ``jwt_algorithm``
    defaults to ``RS256`` and is left there by every standalone install, while
    local mode signs HS256 with the auto-generated secret — so keying off it
    would hand a standalone deployment an RS256 generator with no RSA key at
    all. ``AuthService._algorithm`` resolves the same way, from the same
    setting, which is what makes the two agree.

    Callers that are already gated to one mode (``/auth/login`` behind
    ``require_local_mode``, the OAuth service behind ``oauth_enabled``) may call
    the specific builder above; anything that must work in either mode calls
    this.

    Args:
        settings: FaultMavenSettings (or an equivalent).
        revocation_store: The deployment-wide revocation store (#767).
        private_key: RSA private key resolved by ``AuthService``. Ignored under
            local mode, which signs with the HMAC secret.
        public_key: RSA public key resolved by ``AuthService``. Ignored under
            local mode.

    Raises:
        SigningKeyUnavailableError: Local mode with no ``JWT_SECRET_KEY``
            (``get_settings()`` normally generates and persists one, so this
            means it was explicitly cleared or the persist failed), or OAuth
            mode called without the resolved RSA pair.
    """
    if _auth_mode_name(settings) == "local":
        return build_hs256_token_generator(settings, revocation_store)
    return build_rs256_token_generator(
        settings,
        revocation_store,
        private_key=private_key,
        public_key=public_key,
    )


class ITokenRevocationStore(ABC):
    """Interface for token revocation tracking.

    Two revocation granularities, both served by the one deployment-wide store
    (#767):

    - **Per token** (``add_revoked_token``/``is_revoked``): a single JTI, used
      by logout, OAuth ``/revoke`` and refresh rotation.
    - **Per user** (``revoke_user_tokens_before``/``is_user_revoked``): a
      timestamp watermark, used by the admin revoke-tokens endpoint and the
      deactivate/delete/role-change flows (#769). Enumerating a user's
      outstanding JTIs is not possible — nothing indexes them — so per-user
      revocation records *when* the revocation happened and rejects every
      token issued at or before that instant. It needs no bookkeeping at mint
      time, which is why it covers mint paths added later.

      **That coverage is conditional, not absolute:** matching is on ``sub`` +
      ``iat``, so it holds only while every mint path emits both and keys
      ``sub`` to the same user_id the watermark is written under. A test pins
      this; the type system does not. A future minter that omits ``iat`` would
      under-revoke silently on the generator ``validate_*`` paths, which —
      unlike ``AuthService.verify_token`` — do not ``require`` it. See
      ``docs/architecture/security/iam-design.md`` for the full limits
      (clock skew, Redis-only durability, password-reset tokens).

    Entries carry a TTL matching token expiration; once a token can no longer
    be presented, its revocation entry is redundant and expires.
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
    async def revoke_user_tokens_before(
        self, user_id: str, revoked_at: int, ttl: int
    ) -> None:
        """Set the user's revocation watermark.

        Every token for this user issued at or before ``revoked_at`` becomes
        invalid. Overwrites any earlier watermark — a later revocation is
        strictly stronger, and the caller has already decided to invalidate
        everything outstanding.

        Args:
            user_id: User whose tokens are being revoked
            revoked_at: Revocation instant as a Unix timestamp (seconds)
            ttl: Time to live in seconds; must outlive the longest-lived token
                the deployment issues, or tokens could outlive the watermark
                that revokes them
        """
        ...

    @abstractmethod
    async def is_user_revoked(self, user_id: str, issued_at: int) -> bool:
        """Check a token's ``iat`` against the user's revocation watermark.

        Args:
            user_id: User ID from the token's ``sub`` claim
            issued_at: Token's ``iat`` claim as a Unix timestamp (seconds)

        Returns:
            True if the user has a watermark at or after ``issued_at``
        """
        ...

    @abstractmethod
    async def cleanup_expired(self) -> int:
        """Clean up expired revocation entries.

        Returns:
            Count of entries cleaned up
        """
        ...
