"""User Management Service (TASK-018)

Purpose: Handle user registration, authentication, profile management,
and password operations.

This service provides:
- User registration with email/password
- User authentication with JWT token generation
- Password reset via token-based flow
- Password change (authenticated)
- User profile management
- User deactivation (soft delete)

Design Reference: TASK-018 User Management Service
"""

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import jwt
from redis.asyncio import Redis

from faultmaven.config.settings import get_settings
from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationException,
)
from faultmaven.infrastructure.persistence.user_repository import (
    InMemoryUserRepository,
    User as RepositoryUser,
    UserRepository,
)
from faultmaven.models.auth import TokenPair
from faultmaven.models.rbac import get_permissions_for_roles
from faultmaven.services.auth_service import AuthenticationError, AuthService
from faultmaven.services.base import BaseService
from faultmaven.utils.password import (
    hash_password,
    validate_password_strength,
    verify_password,
)

logger = logging.getLogger(__name__)


# Email validation regex
EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
)

# Password reset token expiry (1 hour)
PASSWORD_RESET_TOKEN_EXPIRY_HOURS = 1

# Redis key prefix for password reset tokens
RESET_TOKEN_PREFIX = "password_reset:"


class UserService(BaseService):
    """User management service.

    Handles all user-related operations including registration,
    authentication, password management, and profile updates.

    Attributes:
        user_repo: User repository for persistence
        auth_service: Auth service for JWT token operations
        redis_client: Redis client for token tracking (optional)
    """

    def __init__(
        self,
        user_repo: UserRepository,
        auth_service: AuthService,
        redis_client: Optional[Redis] = None,
    ):
        """Initialize user service.

        Args:
            user_repo: User repository for persistence
            auth_service: Auth service for JWT token operations
            redis_client: Redis client for token tracking (optional)
        """
        super().__init__("user_service")
        self.user_repo = user_repo
        self.auth_service = auth_service
        self.redis_client = redis_client
        self._settings = get_settings()

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
    # User Authentication
    # ============================================================

    async def authenticate_user(
        self,
        email: str,
        password: str,
        organization_id: Optional[str] = None,
    ) -> Tuple[RepositoryUser, str, str]:
        """Authenticate user with email and password.

        Args:
            email: User's email address
            password: Plain text password
            organization_id: Optional organization context (defaults to default org)

        Returns:
            Tuple of (User, access_token, refresh_token)

        Raises:
            AuthenticationError: Invalid credentials or inactive account

        Workflow:
            1. Get user by email
            2. Verify user exists
            3. Verify user is active
            4. Verify password matches
            5. Update last_login_at timestamp
            6. Get user's roles/permissions
            7. Generate JWT tokens via AuthService
            8. Return (user, access_token, refresh_token)
        """
        self.logger.debug(f"Authenticating user: {email}")

        # Get user by email
        user = await self.user_repo.get_by_email(email)
        if not user:
            self.logger.debug(f"User not found: {email}")
            raise AuthenticationError(
                "Invalid email or password",
                error_code="INVALID_CREDENTIALS",
            )

        # Verify user is active
        if not user.is_active:
            self.logger.debug(f"Inactive user attempted login: {email}")
            raise AuthenticationError(
                "Account is deactivated. Please contact support.",
                error_code="ACCOUNT_INACTIVE",
            )

        # Verify password
        if not user.hashed_password or not verify_password(
            password, user.hashed_password
        ):
            self.logger.debug(f"Invalid password for user: {email}")
            raise AuthenticationError(
                "Invalid email or password",
                error_code="INVALID_CREDENTIALS",
            )

        # Update last login timestamp
        user.last_login_at = datetime.now(timezone.utc)
        user.updated_at = datetime.now(timezone.utc)
        await self.user_repo.save(user)

        # Use default organization if not specified
        org_id = organization_id or "org-default"

        # Get user's roles and permissions
        roles = user.roles if user.roles else ["member"]
        permissions = [p.value for p in get_permissions_for_roles(roles)]

        # Generate JWT tokens
        token_pair = self.auth_service.generate_token_pair(
            user_id=user.user_id,
            organization_id=org_id,
            email=user.email,
            roles=roles,
            permissions=permissions,
        )

        self.logger.info(f"User authenticated successfully: {user.user_id}")

        return user, token_pair.access_token, token_pair.refresh_token

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
            1. Get user by email
            2. If user not found, return dummy token (prevent enumeration)
            3. Generate reset token (JWT with 1 hour expiry)
            4. Store reset token in Redis with TTL
            5. Return reset token
        """
        self.logger.debug(f"Password reset requested for: {email}")

        # Get user by email
        user = await self.user_repo.get_by_email(email)

        if not user:
            # Return a dummy token to prevent email enumeration
            # In production, this would still trigger a "check your email" message
            self.logger.debug(f"Password reset for non-existent email: {email}")
            return self._generate_dummy_reset_token()

        # Generate reset token
        now = datetime.now(timezone.utc)
        expire = now + timedelta(hours=PASSWORD_RESET_TOKEN_EXPIRY_HOURS)
        jti = str(uuid.uuid4())

        claims = {
            "sub": user.user_id,
            "email": user.email,
            "type": "password_reset",
            "iss": self._settings.security.jwt_issuer,
            "aud": self._settings.security.jwt_audience,
            "iat": int(now.timestamp()),
            "exp": int(expire.timestamp()),
            "jti": jti,
        }

        # Encode token using the auth service's private key
        reset_token = self._encode_reset_token(claims)

        # Store token jti in Redis for single-use tracking
        if self.redis_client:
            key = f"{RESET_TOKEN_PREFIX}{jti}"
            ttl_seconds = PASSWORD_RESET_TOKEN_EXPIRY_HOURS * 3600
            await self.redis_client.setex(key, ttl_seconds, user.user_id)

        self.logger.info(f"Password reset token generated for user: {user.user_id}")
        return reset_token

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
            AuthenticationError: Invalid or expired token
            ValidationException: Weak password

        Workflow:
            1. Verify reset token (signature, expiration)
            2. Extract user_id from token
            3. Check token not already used (jti in Redis)
            4. Validate new password strength
            5. Hash new password
            6. Update user record
            7. Revoke all user's JWT tokens (force re-login)
            8. Mark reset token as used (remove from Redis)
            9. Return updated user
        """
        self.logger.debug("Processing password reset")

        # Verify reset token
        try:
            claims = self._verify_reset_token(reset_token)
        except Exception as e:
            self.logger.debug(f"Invalid reset token: {e}")
            raise AuthenticationError(
                "Invalid or expired password reset token",
                error_code="INVALID_RESET_TOKEN",
            )

        user_id = claims.get("sub")
        jti = claims.get("jti")
        token_type = claims.get("type")

        # Verify token type
        if token_type != "password_reset":
            raise AuthenticationError(
                "Invalid token type",
                error_code="INVALID_TOKEN_TYPE",
            )

        # Check token not already used (via Redis)
        if self.redis_client:
            key = f"{RESET_TOKEN_PREFIX}{jti}"
            stored_user_id = await self.redis_client.get(key)
            if not stored_user_id:
                raise AuthenticationError(
                    "Password reset token has already been used or expired",
                    error_code="TOKEN_ALREADY_USED",
                )
            # Mark token as used by deleting it
            await self.redis_client.delete(key)

        # Validate new password strength
        validate_password_strength(new_password)

        # Get user
        user = await self.user_repo.get(user_id)
        if not user:
            raise AuthenticationError(
                "User not found",
                error_code="USER_NOT_FOUND",
            )

        # Hash new password and update
        user.hashed_password = hash_password(new_password)
        user.last_password_change_at = datetime.now(timezone.utc)
        user.updated_at = datetime.now(timezone.utc)

        # Save user
        updated_user = await self.user_repo.save(user)

        # Revoke all user's JWT tokens (force re-login)
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

        # Revoke all user's JWT tokens (force re-login)
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
            self.logger.info(f"Email updated for user: {user_id} (verification required)")
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
        """Deactivate user account (soft delete).

        Args:
            user_id: User's ID

        Returns:
            Deactivated User object

        Raises:
            NotFoundError: User not found

        Workflow:
            1. Get user by ID
            2. Set is_active=False
            3. Revoke all user's JWT tokens
            4. Update updated_at timestamp
            5. Save user record
            6. Return deactivated user
        """
        self.logger.info(f"Deactivating user: {user_id}")

        # Get user
        user = await self.user_repo.get(user_id)
        if not user:
            raise NotFoundError("User", user_id)

        # Deactivate
        user.is_active = False
        user.deleted_at = datetime.now(timezone.utc)
        user.updated_at = datetime.now(timezone.utc)

        # Save user
        deactivated_user = await self.user_repo.save(user)

        # Revoke all user's JWT tokens
        await self.auth_service.revoke_user_tokens(user_id)

        self.logger.info(f"User deactivated: {user_id}")
        return deactivated_user

    async def activate_user(
        self,
        user_id: str,
    ) -> RepositoryUser:
        """Reactivate user account.

        Args:
            user_id: User's ID

        Returns:
            Activated User object

        Raises:
            NotFoundError: User not found
        """
        self.logger.info(f"Activating user: {user_id}")

        # Get user
        user = await self.user_repo.get(user_id)
        if not user:
            raise NotFoundError("User", user_id)

        # Activate
        user.is_active = True
        user.deleted_at = None
        user.updated_at = datetime.now(timezone.utc)

        # Save user
        activated_user = await self.user_repo.save(user)

        self.logger.info(f"User activated: {user_id}")
        return activated_user

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
        limit: int = 50,
        offset: int = 0,
        is_active: Optional[bool] = None,
    ) -> Tuple[List[RepositoryUser], int]:
        """List users with pagination and optional filtering.

        Args:
            limit: Maximum results
            offset: Pagination offset
            is_active: Filter by active status

        Returns:
            Tuple of (users, total_count)
        """
        return await self.user_repo.list_users(
            limit=limit,
            offset=offset,
            is_active=is_active,
        )

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

    def _generate_dummy_reset_token(self) -> str:
        """Generate a dummy reset token for non-existent emails.

        This prevents email enumeration by returning a valid-looking
        token even when the email doesn't exist.

        Returns:
            Dummy token string
        """
        # Generate a random token that looks valid but won't verify
        now = datetime.now(timezone.utc)
        expire = now + timedelta(hours=PASSWORD_RESET_TOKEN_EXPIRY_HOURS)

        claims = {
            "sub": str(uuid.uuid4()),  # Random user ID
            "email": "dummy@dummy.local",
            "type": "password_reset",
            "iss": self._settings.security.jwt_issuer,
            "aud": self._settings.security.jwt_audience,
            "iat": int(now.timestamp()),
            "exp": int(expire.timestamp()),
            "jti": str(uuid.uuid4()),
        }

        return self._encode_reset_token(claims)

    def _encode_reset_token(self, claims: Dict[str, Any]) -> str:
        """Encode reset token claims into JWT.

        Args:
            claims: Token claims

        Returns:
            Encoded JWT token
        """
        # Use the auth service's key for consistency
        return jwt.encode(
            claims,
            self.auth_service._private_key,
            algorithm=self._settings.security.jwt_algorithm,
        )

    def _verify_reset_token(self, token: str) -> Dict[str, Any]:
        """Verify and decode reset token.

        Args:
            token: JWT token

        Returns:
            Decoded claims

        Raises:
            jwt.InvalidTokenError: Invalid token
        """
        return jwt.decode(
            token,
            self.auth_service._public_key,
            algorithms=[self._settings.security.jwt_algorithm],
            issuer=self._settings.security.jwt_issuer,
            audience=self._settings.security.jwt_audience,
        )
