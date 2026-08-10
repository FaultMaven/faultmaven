"""User Management Service (TASK-018, TASK-019)

Purpose: Handle user registration, profile management, password operations,
and admin user management.

This service mints no tokens. Its `authenticate_user` — the only caller of the
parallel token-mint path on `AuthService` — reached no route and was removed
with that path in #853; sign-in mints through `IJWTTokenGenerator`.

This service provides:
- User registration with email/password
- Password reset via token-based flow
- Password change (authenticated)
- User profile management
- User deactivation (soft delete)
- Role assignment and removal (TASK-019)

Design Reference: TASK-018 User Management Service, TASK-019 Admin User Management
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from faultmaven.config.settings import get_settings

# Interface imports for clean architecture compliance
# Redis type is for DI signatures only — the actual client is injected at runtime
if TYPE_CHECKING:
    from redis.asyncio import Redis

    from faultmaven.models.interfaces import IVectorStore

from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    InactiveAccountError,
    NotFoundError,
    ServiceError,
    ValidationException,
)
from faultmaven.infrastructure.persistence.user_repository import (
    InMemoryUserRepository,
    UserRepository,
)
from faultmaven.infrastructure.persistence.user_repository import User as RepositoryUser
from faultmaven.models.rbac import Role, get_permissions_for_roles
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    PASSWORD_RESET_TOKEN_EXPIRY_HOURS,
    PasswordResetMint,
)
from faultmaven.services.base import BaseService
from faultmaven.utils.password import (
    hash_password,
    validate_password_strength,
    verify_password,
)


# Dynamic import helper for AuthenticationError (avoid import-linter violation)
def _get_authentication_error():
    """Lazy import of AuthenticationError to avoid import-linter violations."""
    import importlib

    auth_service_module = importlib.import_module(
        "faultmaven.modules.auth.domain.services.auth_service"
    )
    return auth_service_module.AuthenticationError


logger = logging.getLogger(__name__)


# Email validation regex
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

# Password reset token expiry (PASSWORD_RESET_TOKEN_EXPIRY_HOURS) is imported
# from the generator that signs the token — one home for the constant, so the
# Redis TTL below and the token's own `exp` cannot drift (#959).

# Redis key prefix for password reset tokens
RESET_TOKEN_PREFIX = "password_reset:"

# The single observable every unusable reset link produces: unknown user,
# deactivated account, revoked token, already-used token, bad signature. They
# differ only in the log line.
#
# Distinguishing them tells whoever submitted the link whether an account exists
# and what state it is in — the enumeration `generate_dummy_reset_token` exists
# to prevent, since a dummy token is otherwise identifiable by the error it
# provokes. A reset link proves nothing about who is holding it — it can be
# captured in transit — so nothing about the account may be inferred from the
# refusal. `/auth/refresh` draws the same line, collapsing "no longer exists or
# is inactive" into one response.
RESET_REFUSED_CODE = "INVALID_RESET_TOKEN"
RESET_REFUSED_MESSAGE = (
    "Password reset link is invalid or has expired. Please request a new one."
)


class UserService(BaseService):
    """User management service.

    Handles all user-related operations including registration,
    authentication, password management, profile updates, and
    role management (TASK-019).

    Attributes:
        user_repo: User repository for persistence
        auth_service: Auth service for token revocation operations
        token_generator: The deployment's IJWTTokenGenerator — the only thing
            in the process that signs (#959). ``None`` when the deployment has
            no usable signing key; see ``__init__``.
        redis_client: Redis client for token tracking (optional)
    """

    def __init__(
        self,
        user_repo: UserRepository,
        auth_service: Any,
        token_generator: Any = None,
        redis_client: Optional[Redis] = None,
    ):
        """Initialize user service.

        Args:
            user_repo: User repository for persistence
            auth_service: Auth service for revocation operations (required)
            token_generator: IJWTTokenGenerator for the reset-token surface.
                Optional, and deliberately so: a deployment with no usable
                signing key can still list, create, update and deactivate
                users, and refusing to construct this service would take the
                admin routes down over a capability they never touch. The
                reset flow — and only it — refuses while this is ``None``.
            redis_client: Redis client for token tracking (optional)

        Raises:
            ValueError: If required dependencies are not provided
        """
        super().__init__("user_service")
        self.user_repo = user_repo

        # Require explicit dependency injection
        if auth_service is None:
            raise ValueError("auth_service is required for UserService")

        self.auth_service = auth_service
        self.token_generator = token_generator

        self.redis_client = redis_client
        self._settings = get_settings()

    def _signer(self) -> Any:
        """Return the token generator, or refuse the whole reset flow.

        Called at both reset entry points, BEFORE anything reads the request:
        the refusal must not depend on which address was asked about, or it
        becomes the oracle the decoy exists to prevent. Failing here also keeps
        ``None`` from travelling further in — nothing downstream has to
        remember that the signer might be absent.

        A decoy would be the wrong answer in this state. It is a token that can
        never be redeemed, handed to a real user alongside "check your email":
        a fabricated success, which is the one thing this system does not do.
        An error is honest, uniform, and actionable by the operator whose
        ``JWT_SECRET_KEY`` never got written.
        """
        if self.token_generator is None:
            raise ServiceError(
                "Password reset is unavailable: this deployment has no JWT "
                "signing key configured."
            )
        return self.token_generator

    # ============================================================
    # User Registration
    # ============================================================

    async def register_user(
        self,
        email: str,
        password: str,
        full_name: str,
    ) -> RepositoryUser:
        """Register a new user account.

        Args:
            email: User's email address (must be unique)
            password: Plain text password (will be hashed)
            full_name: User's full name

        Returns:
            Created User object

        Raises:
            ValidationException: Invalid email format or weak password
            ConflictError: Email already registered

        Password Requirements:
            - Minimum 8 characters
            - At least one uppercase letter
            - At least one lowercase letter
            - At least one digit
            - At least one special character

        Workflow:
            1. Validate email format
            2. Validate password strength
            3. Check email not already registered
            4. Hash password with bcrypt (cost factor 12)
            5. Create user record
            6. Return created user
        """
        self.logger.info(f"Registering new user: {email}")

        # Validate email format
        self._validate_email(email)

        # Validate password strength
        validate_password_strength(password)

        # Check email not already registered
        existing_user = await self.user_repo.get_by_email(email)
        if existing_user:
            raise ConflictError("Email already registered")

        # Hash password
        hashed_password = hash_password(password)

        # Create user
        now = datetime.now(timezone.utc)
        user = RepositoryUser(
            user_id=str(uuid.uuid4()),
            username=email.split("@")[0],  # Use email prefix as username
            email=email,
            display_name=full_name,
            hashed_password=hashed_password,
            is_active=True,
            is_email_verified=False,  # Not verified until email confirmation
            created_at=now,
            updated_at=now,
            roles=["member"],  # Default role
        )

        # Save user
        try:
            created_user = await self.user_repo.create(user)
            self.logger.info(f"User registered successfully: {created_user.user_id}")
            return created_user
        except ConflictError:
            # Re-raise conflict errors
            raise
        except Exception as e:
            self.logger.error(f"Failed to register user: {e}")
            raise

    # ============================================================
    # Password Reset
    # ============================================================

    async def request_password_reset(
        self,
        email: str,
    ) -> str:
        """Request password reset token.

        Args:
            email: User's email address

        Returns:
            Password reset token (to be sent via email)

        Note:
            Returns a token even if email not found (prevents email enumeration).
            In production, the token should be sent via email, not returned directly.

        Workflow:
            1. Refuse outright if this deployment cannot sign (uniformly, before
               anything looks at the address)
            2. Mint — a real token for a live account, an indistinguishable
               decoy for an unknown address or a deactivated one
            3. File the mint's jti in Redis with TTL (single-use tracking)
            4. Return the token

        The three outcomes — real account, unknown email, deactivated account —
        must be indistinguishable to whoever receives the token, who can read
        every claim in it: all three return a token of the same shape, with the
        same claim values (including the address that was asked about), signed
        with the same key. A deactivated account is refused at the mint
        chokepoint (`_refuse_if_deactivated`) and falls back to the same decoy
        an unknown address gets; its holder is then refused at redemption by the
        active-account check in `reset_password`, which is where the refusal
        belongs — a reset link proves nothing about who holds it.

        Indistinguishable includes the I/O. Steps 2 and 3 run for all three
        outcomes through one code path, so a Redis fault fails every one of them
        the same way. When only the real branch wrote to Redis, an outage turned
        this method into the oracle the decoy exists to prevent: a registered
        address raised while an unregistered one returned a token. A decoy's key
        is filed under its own random jti and stores its own random subject, so
        the store holds nothing that names an account, and redeeming one dies at
        the user lookup — the same generic refusal every unusable link produces.
        """
        signer = self._signer()

        self.logger.debug(f"Password reset requested for: {email}")

        # #831: capture before the account lookup. One instant for both
        # branches — real and decoy mints must not differ by the lookup's
        # latency in ``iat``.
        state_read_at = datetime.now(timezone.utc)

        mint = await self._mint_reset_token(signer, email, state_read_at)

        # ONE write, reached by all three outcomes. Two write sites that must
        # agree is how the asymmetry got there the first time.
        key = f"{RESET_TOKEN_PREFIX}{mint.jti}"
        ttl_seconds = PASSWORD_RESET_TOKEN_EXPIRY_HOURS * 3600
        await self.redis_client.setex(key, ttl_seconds, mint.subject)

        return mint.token

    async def _mint_reset_token(
        self, signer: Any, email: str, state_read_at: datetime
    ) -> PasswordResetMint:
        """Mint the reset token for an address: real if it can be, decoy if not.

        The jti is taken from the minter rather than read back off the token:
        this service holds no key, and the alternatives are decoding without
        verification or verifying something signed microseconds ago — a
        per-request signature check that asserts nothing the mint did not
        already know.
        """
        user = await self.user_repo.get_by_email(email)

        if not user:
            # In production this still answers "check your email" — the caller
            # cannot tell this branch from the one above.
            self.logger.debug(f"Password reset for non-existent email: {email}")
            return await signer.generate_dummy_reset_token(
                email, state_read_at=state_read_at
            )

        try:
            mint = await signer.generate_password_reset_token(
                user, state_read_at=state_read_at
            )
        except InactiveAccountError:
            # No live credential for a deactivated account — and no observable
            # that says so.
            self.logger.info(
                "Password reset refused at mint",
                extra={"refusal_reason": "account_inactive", "user_id": user.user_id},
            )
            return await signer.generate_dummy_reset_token(
                email, state_read_at=state_read_at
            )

        self.logger.info(f"Password reset token generated for user: {user.user_id}")
        return mint

    def _refuse_reset(self, reason: str, **log_context) -> Exception:
        """Return the one refusal every unusable reset link produces.

        The specific reason reaches the logs only — see RESET_REFUSED_CODE for
        why the caller is told nothing that distinguishes them.
        """
        self.logger.info(
            "Password reset refused",
            extra={"refusal_reason": reason, **log_context},
        )
        AuthenticationError = _get_authentication_error()
        return AuthenticationError(
            RESET_REFUSED_MESSAGE,
            error_code=RESET_REFUSED_CODE,
        )

    async def reset_password(
        self,
        reset_token: str,
        new_password: str,
    ) -> RepositoryUser:
        """Reset user password with reset token.

        Args:
            reset_token: Password reset token (from email)
            new_password: New plain text password

        Returns:
            Updated User object

        Raises:
            AuthenticationError: Invalid, expired or revoked token; inactive
                account
            ValidationException: Weak password

        Workflow:
            1. Verify reset token (signature, expiration)
            2. Extract user_id from token
            3. Check the token is not revoked (same rule as every other token)
            4. Validate new password strength
            5. Load the user and require an active account
            6. Consume the one-time token (atomic; last, so a refused attempt
               does not destroy a usable link)
            7. Hash new password
            8. Update user record
            9. Revoke all user's JWT tokens (force re-login)
            10. Return updated user
        """
        self.logger.debug("Processing password reset")

        # Verify reset token. Signature, issuer, audience, expiry AND the
        # `password_reset` type are all the generator's answer (#959) — this
        # service holds no key, so a wrong-type token arrives here as the same
        # InvalidTokenError as a forged one and collapses into the one refusal
        # every unusable link produces. With no signer at all there is nothing
        # to verify against, and `_signer` refuses before the token is read:
        # accepting one on a deployment that cannot sign is not a thing to do
        # quietly.
        signer = self._signer()

        try:
            claims = await signer.verify_password_reset_token(reset_token)
        except Exception as e:
            raise self._refuse_reset("token_unverifiable", error=str(e))

        user_id = claims.get("sub")
        jti = claims.get("jti")

        # Apply the SAME revocation rule as access and refresh tokens: reset
        # tokens carry `sub`, `iat` and `jti`, so both arms work unchanged, and
        # "revoke all tokens for this user" must not leave an outstanding reset
        # link alive (#829). One rule for every token type is the point — a
        # per-flow cleanup would be the fragmentation #767 removed.
        #
        # Checked BEFORE the one-time key is burned, so a store outage cannot
        # destroy a legitimate token, and deliberately not fail-open: unlike the
        # request path (short-lived access tokens, availability first), a reset
        # is an account-takeover-grade operation and an unknown revocation state
        # must refuse rather than proceed.
        reason = await self.auth_service.get_revocation_reason(claims)
        if reason:
            raise self._refuse_reset(reason, user_id=user_id, jti=jti)

        # Validate new password strength
        validate_password_strength(new_password)

        # Get user
        user = await self.user_repo.get(user_id)
        if not user:
            raise self._refuse_reset("user_not_found", user_id=user_id)

        # A deactivated account must not be recoverable through a reset link
        # issued before it was deactivated — the same posture /auth/refresh
        # takes, and the same one `_refuse_if_deactivated` enforces at the
        # `IJWTTokenGenerator` chokepoint every mint path funnels through
        # (#829).
        if not user.is_active:
            raise self._refuse_reset("account_inactive", user_id=user_id)

        # Consume the one-time token LAST, once every other check has passed:
        # burning it earlier destroys a legitimate reset link on an attempt that
        # was going to be refused anyway (a weak password, a deactivated
        # account), leaving the user with nothing to retry.
        #
        # DELETE reports how many keys it removed, so the check and the burn are
        # one atomic operation. A read-then-delete would let two concurrent
        # requests both observe the key and both reset the password.
        key = f"{RESET_TOKEN_PREFIX}{jti}"
        if not await self.redis_client.delete(key):
            raise self._refuse_reset("token_already_used", user_id=user_id, jti=jti)

        # Hash new password and update
        user.hashed_password = hash_password(new_password)
        user.last_password_change_at = datetime.now(timezone.utc)
        user.updated_at = datetime.now(timezone.utc)

        # Save user
        updated_user = await self.user_repo.save(user)

        # Persist FIRST, then revoke. Revoking first opens a TOCTOU: during the
        # gap the DB still holds the OLD password/roles/active flag, so a login
        # landing in it mints a token with `iat` AFTER the watermark that
        # carries pre-change state — surviving the very revocation meant to
        # kill it. Saving first inverts that: anything minted in the gap has
        # `iat` at or before the watermark and dies with it. The cost is that a
        # store-write failure leaves the change committed while reporting an
        # error (#767 posture: never report a revocation that did not land).
        await self.auth_service.revoke_user_tokens(user_id)

        self.logger.info(f"Password reset successfully for user: {user_id}")
        return updated_user

    # ============================================================
    # Password Change
    # ============================================================

    async def change_password(
        self,
        user_id: str,
        current_password: str,
        new_password: str,
    ) -> RepositoryUser:
        """Change user password (authenticated).

        Args:
            user_id: User's ID
            current_password: Current password for verification
            new_password: New password

        Returns:
            Updated User object

        Raises:
            NotFoundError: User not found
            AuthenticationError: Current password incorrect
            ValidationException: Weak new password

        Workflow:
            1. Get user by ID
            2. Verify current password
            3. Validate new password strength
            4. Hash new password
            5. Update user record
            6. Revoke all user's JWT tokens (force re-login)
            7. Return updated user
        """
        self.logger.debug(f"Password change requested for user: {user_id}")

        # Get user
        user = await self.user_repo.get(user_id)
        if not user:
            raise NotFoundError("User", user_id)

        # Verify current password
        if not user.hashed_password or not verify_password(
            current_password, user.hashed_password
        ):
            AuthenticationError = _get_authentication_error()
            raise AuthenticationError(
                "Current password is incorrect",
                error_code="INVALID_PASSWORD",
            )

        # Validate new password strength
        validate_password_strength(new_password)

        # Hash new password and update
        user.hashed_password = hash_password(new_password)
        user.last_password_change_at = datetime.now(timezone.utc)
        user.updated_at = datetime.now(timezone.utc)

        # Save user
        updated_user = await self.user_repo.save(user)

        # Persist first, then revoke — see reset_password for why the reverse
        # order opens a TOCTOU.
        await self.auth_service.revoke_user_tokens(user_id)

        self.logger.info(f"Password changed successfully for user: {user_id}")
        return updated_user

    # ============================================================
    # User Profile Management
    # ============================================================

    async def update_user_profile(
        self,
        user_id: str,
        email: Optional[str] = None,
        full_name: Optional[str] = None,
    ) -> RepositoryUser:
        """Update user profile information.

        Args:
            user_id: User's ID
            email: New email (optional)
            full_name: New full name (optional)

        Returns:
            Updated User object

        Raises:
            NotFoundError: User not found
            ValidationException: Invalid email format
            ConflictError: Email already in use

        Workflow:
            1. Get user by ID
            2. If email changed:
               - Validate email format
               - Check email not already used
               - Update email
               - Set is_verified=False (require re-verification)
            3. If full_name changed:
               - Update full_name
            4. Update updated_at timestamp
            5. Save user record
            6. Return updated user
        """
        self.logger.debug(f"Updating profile for user: {user_id}")

        # Get user
        user = await self.user_repo.get(user_id)
        if not user:
            raise NotFoundError("User", user_id)

        # Track if email changed
        email_changed = False

        # Update email if provided and different
        if email and email.lower() != user.email.lower():
            # Validate email format
            self._validate_email(email)

            # Check email not already used
            existing = await self.user_repo.get_by_email(email)
            if existing and existing.user_id != user_id:
                raise ConflictError("Email already in use")

            user.email = email
            user.is_email_verified = False  # Require re-verification
            email_changed = True

        # Update full_name if provided
        if full_name and full_name != user.display_name:
            user.display_name = full_name

        # Update timestamp
        user.updated_at = datetime.now(timezone.utc)

        # Save user
        updated_user = await self.user_repo.save(user)

        if email_changed:
            self.logger.info(
                f"Email updated for user: {user_id} (verification required)"
            )
        else:
            self.logger.info(f"Profile updated for user: {user_id}")

        return updated_user

    # ============================================================
    # User Deactivation
    # ============================================================

    async def deactivate_user(
        self,
        user_id: str,
    ) -> RepositoryUser:
        """Deactivate user account (soft delete)."""
        self.logger.info(f"Deactivating user: {user_id}")

        user = await self.user_repo.get(user_id)
        if not user:
            raise NotFoundError("User", user_id)

        user.is_active = False
        user.deleted_at = datetime.now(timezone.utc)
        user.updated_at = datetime.now(timezone.utc)

        deactivated_user = await self.user_repo.save(user)

        # Persist first, then revoke — see reset_password for why the reverse
        # order opens a TOCTOU.
        await self.auth_service.revoke_user_tokens(user_id)
        return deactivated_user

    async def deactivate_user_admin(
        self,
        user_id: str,
        organization_id: str,
        admin_user_id: str,
    ) -> RepositoryUser:
        """Deactivate user account (admin-only, soft delete)."""
        if admin_user_id == user_id:
            raise AuthorizationError("Cannot deactivate your own account")

        user = await self.user_repo.get(user_id)
        if not user:
            raise NotFoundError("User", user_id)
        if not user.is_active:
            raise ConflictError("User already deactivated")

        return await self.deactivate_user(user_id)

    async def activate_user(
        self,
        user_id: str,
    ) -> RepositoryUser:
        """Reactivate user account."""
        self.logger.info(f"Activating user: {user_id}")

        user = await self.user_repo.get(user_id)
        if not user:
            raise NotFoundError("User", user_id)
        if user.is_active:
            raise ConflictError("User already active")

        user.is_active = True
        user.deleted_at = None
        user.updated_at = datetime.now(timezone.utc)
        return await self.user_repo.save(user)

    async def activate_user_admin(
        self,
        user_id: str,
        organization_id: str,
        admin_user_id: str,
    ) -> RepositoryUser:
        """Reactivate user account (admin-only)."""
        return await self.activate_user(user_id)

    # ============================================================
    # User Lookup
    # ============================================================

    async def get_user(
        self,
        user_id: str,
    ) -> Optional[RepositoryUser]:
        """Get user by ID.

        Args:
            user_id: User's ID

        Returns:
            User if found, None otherwise
        """
        return await self.user_repo.get(user_id)

    async def get_user_by_email(
        self,
        email: str,
    ) -> Optional[RepositoryUser]:
        """Get user by email.

        Args:
            email: Email address

        Returns:
            User if found, None otherwise
        """
        return await self.user_repo.get_by_email(email)

    async def list_users(
        self,
        organization_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        is_active: Optional[bool] = None,
        role: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Tuple[List[RepositoryUser], int]:
        """List users with pagination and optional filtering.

        Args:
            organization_id: Organization context for scoping (not yet enforced in in-memory repo)
            limit: Maximum results
            offset: Pagination offset
            is_active: Filter by active status
            role: Filter by role (admin, member, viewer) - TASK-019
            search: Search by email or name (case-insensitive) - TASK-019

        Returns:
            Tuple of (users, total_count)
        """
        # Get base users list from repository
        users, total = await self.user_repo.list_users(
            limit=1000,  # Get all for filtering
            offset=0,
            is_active=is_active,
        )

        # Apply additional filters (TASK-019)
        filtered_users = []
        for user in users:
            # Ensure is_active filtering even if repository doesn't apply it
            if is_active is not None and user.is_active != is_active:
                continue

            # Filter by role
            if role is not None:
                user_roles = user.roles if user.roles else ["member"]
                if role not in user_roles:
                    continue

            # Filter by search (case-insensitive partial match)
            if search is not None:
                search_lower = search.lower()
                email_match = search_lower in user.email.lower()
                name_match = (
                    user.display_name and search_lower in user.display_name.lower()
                )
                if not (email_match or name_match):
                    continue

            filtered_users.append(user)

        # Calculate total after filtering
        total = len(filtered_users)

        # Apply pagination
        paginated_users = filtered_users[offset : offset + limit]

        return paginated_users, total

    # ============================================================
    # Role Management (TASK-019)
    # ============================================================

    async def get_user_with_metadata(
        self,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get user with additional metadata (TASK-019).

        Returns user dict with:
        - All User fields (except hashed_password)
        - permissions (derived from roles)
        - metadata.login_count (if tracked)
        - metadata.failed_login_attempts (if tracked)

        Args:
            user_id: User identifier

        Returns:
            User dict with metadata, or None if not found
        """
        user = await self.user_repo.get(user_id)

        if not user:
            return None

        # Derive permissions from roles
        user_roles = user.roles if user.roles else ["member"]
        permissions = [p.value for p in get_permissions_for_roles(user_roles)]

        return {
            "user_id": user.user_id,
            "organization_id": "org-default",
            "email": user.email,
            "full_name": user.display_name,
            "roles": user_roles,
            "permissions": sorted(permissions),
            "is_active": user.is_active,
            "is_verified": user.is_email_verified,
            "last_login_at": (
                user.last_login_at.isoformat() if user.last_login_at else None
            ),
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "updated_at": user.updated_at.isoformat() if user.updated_at else None,
            "metadata": {
                "login_count": 0,  # TODO: Track in repository
                "failed_login_attempts": 0,  # TODO: Track in repository
            },
        }

    async def assign_role(
        self,
        user_id: str,
        role: str,
        organization_id: str,
        admin_user_id: str,
    ) -> RepositoryUser:
        """Assign role to user (TASK-019).

        Args:
            user_id: Target user ID
            role: Role to assign (admin, member, viewer)
            organization_id: Organization context for authorization (required)
            admin_user_id: Admin performing the action (cannot be same as user_id)

        Returns:
            Updated User

        Raises:
            NotFoundError: User not found
            AuthorizationError: admin_user_id == user_id (self-modification)
            ValidationException: Invalid role
            ConflictError: User already has this role
        """
        # Prevent self-modification
        if admin_user_id == user_id:
            raise AuthorizationError("Cannot modify your own roles")

        # Validate role
        valid_roles = [r.value for r in Role]
        if role not in valid_roles:
            raise ValidationException(
                f"Invalid role: {role}. Valid roles are: {', '.join(valid_roles)}"
            )

        user = await self.user_repo.get(user_id)
        if not user:
            raise NotFoundError("User", user_id)

        # Check if user already has this role
        current_roles = user.roles if user.roles else ["member"]
        if current_roles == [role]:
            raise ConflictError(
                f"User already has role '{role}'",
                resource_type="User",
                resource_id=user_id,
                conflict_reason="role_already_assigned",
            )

        # Assign new role (replaces existing)
        user.roles = [role]
        user.updated_at = datetime.now(timezone.utc)
        updated_user = await self.user_repo.save(user)

        # Revoke AFTER persisting (roles changed, tokens stale). Revoking first
        # would let a login in the gap mint a token carrying the OLD role with
        # an `iat` past the watermark — see reset_password.
        revoked_before = await self.auth_service.revoke_user_tokens(user_id)

        self.logger.info(
            f"Role assigned: {user_id} -> {role}, "
            f"tokens revoked before: {revoked_before.isoformat()}"
        )
        return updated_user

    async def remove_role(
        self,
        user_id: str,
        role: str,
        organization_id: str,
        admin_user_id: str,
    ) -> RepositoryUser:
        """Remove role from user (TASK-019).

        Downgrades user to viewer role (minimum privilege).

        Args:
            user_id: Target user ID
            role: Role to remove (admin, member)
            organization_id: Organization context for authorization (required)
            admin_user_id: Admin performing the action (cannot be same as user_id)

        Returns:
            Updated User (with viewer role)

        Raises:
            NotFoundError: User not found OR user doesn't have this role
            AuthorizationError: admin_user_id == user_id (self-modification)
            ValidationException: Attempting to remove viewer role (minimum privilege)
        """
        # Prevent self-modification
        if admin_user_id == user_id:
            raise AuthorizationError("Cannot modify your own roles")

        # Cannot remove viewer role (minimum privilege level)
        if role == Role.VIEWER.value:
            raise ValidationException(
                "Cannot remove viewer role. Viewer is the minimum privilege level."
            )

        # Validate role
        valid_roles = [r.value for r in Role]
        if role not in valid_roles:
            raise ValidationException(
                f"Invalid role: {role}. Valid roles are: {', '.join(valid_roles)}"
            )

        user = await self.user_repo.get(user_id)
        if not user:
            raise NotFoundError("User", user_id)

        # Check if user has this role
        current_roles = user.roles if user.roles else ["member"]
        if role not in current_roles:
            raise NotFoundError("Role", f"{user_id}/{role}")

        # Downgrade to viewer (minimum privilege)
        user.roles = [Role.VIEWER.value]
        user.updated_at = datetime.now(timezone.utc)
        updated_user = await self.user_repo.save(user)

        # Revoke AFTER persisting: revoking first would let a login in the gap
        # mint a token carrying the ELEVATED role with an `iat` past the
        # watermark — see reset_password.
        revoked_before = await self.auth_service.revoke_user_tokens(user_id)

        self.logger.info(
            f"Role removed: {user_id}, role={role}, downgraded to viewer, "
            f"tokens revoked before: {revoked_before.isoformat()}"
        )
        return updated_user

    # ============================================================
    # Helper Methods
    # ============================================================

    def _validate_email(self, email: str) -> None:
        """Validate email format.

        Args:
            email: Email to validate

        Raises:
            ValidationException: Invalid email format
        """
        if not email or not EMAIL_REGEX.match(email):
            raise ValidationException("Invalid email format")

    # `_encode_reset_token`, `_verify_reset_token` and
    # `_generate_dummy_reset_token` lived here until #959. They signed with
    # `auth_service._private_key` — another service's private attribute —
    # paired with `security.jwt_algorithm`, a combination nothing kept
    # coherent: under `JWT_ALGORITHM=HS256` that is an RSA PEM handed to an
    # HMAC signer, which PyJWT refuses. The reset surface now lives on
    # `IJWTTokenGenerator`, where the key and the algorithm are the same
    # object's.
