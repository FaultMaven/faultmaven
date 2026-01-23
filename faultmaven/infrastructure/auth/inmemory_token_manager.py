"""In-Memory Token Management System

Purpose: In-memory implementation of token management for local development

This module provides a RAM-based token manager that stores tokens in Python
dictionaries. Data is lost on application restart, making it suitable for
local development and testing where Redis is not available.

Key Features:
- Secure token generation using UUID
- SHA-256 token hashing for storage
- Automatic expiration handling
- Token usage tracking
- Cleanup of expired tokens

Storage:
- All data stored in Python dictionaries (RAM)
- Data lost on application restart
- Thread-safe using asyncio.Lock
"""

import asyncio
import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any

from faultmaven.models.auth import (
    AuthToken,
    DevUser,
    TokenStatus,
    TokenValidationResult,
)

logger = logging.getLogger(__name__)


class InMemoryTokenManager:
    """In-memory token management system

    Manages authentication tokens using Python dictionaries.
    Suitable for local development and testing.

    Token Storage Schema (in-memory):
    - _tokens: Dict[token_hash, user_id] - Active tokens only
    - _token_metadata: Dict[token_id, AuthToken dict] - All tokens (including revoked)
    - _user_tokens: Dict[user_id, List[token_id]] - User's token IDs
    - _expiry_times: Dict[token_hash, datetime] - Expiration tracking
    - _token_hash_to_id: Dict[token_hash, token_id] - Reverse lookup for O(1) cleanup
    """

    def __init__(self, user_store: Optional[Any] = None):
        """Initialize in-memory token manager

        Args:
            user_store: Optional user store for user lookup during validation.
                       If None, will be retrieved from container when needed.
        """
        self.token_expiry_seconds = 24 * 60 * 60  # 24 hours
        self.cleanup_batch_size = 100
        self._user_store = user_store  # Injected dependency

        # In-memory storage
        self._tokens: Dict[str, str] = {}  # token_hash -> user_id (active tokens only)
        self._token_metadata: Dict[str, dict] = (
            {}
        )  # token_id -> AuthToken dict (all tokens)
        self._user_tokens: Dict[str, List[str]] = {}  # user_id -> [token_id, ...]
        self._expiry_times: Dict[str, datetime] = {}  # token_hash -> expiry datetime
        self._token_hash_to_id: Dict[str, str] = (
            {}
        )  # token_hash -> token_id (for O(1) lookup)
        self._lock = asyncio.Lock()  # Thread-safe operations

    async def create_token(self, user: DevUser) -> str:
        """Generate and store a new authentication token

        Args:
            user: User to create token for

        Returns:
            Generated token string (UUID)

        Raises:
            Exception: If token creation fails
        """
        try:
            async with self._lock:
                return self._create_token_unsafe(user)
        except Exception as e:
            logger.error(f"Failed to create token for user {user.user_id}: {e}")
            raise

    def _create_token_unsafe(self, user: DevUser) -> str:
        """Internal token creation (assumes lock is held)"""
        # Generate new token
        token = str(uuid.uuid4())
        token_id = str(uuid.uuid4())
        token_hash = self._hash_token(token)

        # Create token metadata
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self.token_expiry_seconds
        )
        auth_token = AuthToken(
            token_id=token_id,
            user_id=user.user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            created_at=datetime.now(timezone.utc),
        )

        # Store in memory
        self._tokens[token_hash] = user.user_id
        self._token_metadata[token_id] = auth_token.to_dict()
        self._expiry_times[token_hash] = expires_at
        self._token_hash_to_id[token_hash] = token_id

        # Add to user's token list
        if user.user_id not in self._user_tokens:
            self._user_tokens[user.user_id] = []
        self._user_tokens[user.user_id].append(token_id)

        logger.info(f"Created token for user {user.user_id} (token_id: {token_id})")
        return token

    async def validate_token(self, token: str) -> TokenValidationResult:
        """Validate authentication token and return user

        Args:
            token: Token string to validate

        Returns:
            TokenValidationResult with status and user info
        """
        try:
            if not token:
                return TokenValidationResult(
                    status=TokenStatus.INVALID, error_message="Token is empty"
                )

            token_hash = self._hash_token(token)

            # Acquire lock for all validation operations
            async with self._lock:
                # Check if token exists in active tokens
                user_id = self._tokens.get(token_hash)

                # If not in active tokens, check metadata (might be revoked)
                if not user_id:
                    token_id = self._token_hash_to_id.get(token_hash)
                    if token_id and token_id in self._token_metadata:
                        # Token exists but is not active - check if revoked
                        meta_dict = self._token_metadata[token_id]
                        token_meta = AuthToken.from_dict(meta_dict)
                        if token_meta.is_revoked:
                            return TokenValidationResult(
                                status=TokenStatus.REVOKED,
                                error_message="Token has been revoked",
                            )
                        # Not revoked but not active - expired or invalid
                        if token_meta.is_expired:
                            return TokenValidationResult(
                                status=TokenStatus.EXPIRED,
                                error_message="Token has expired",
                            )

                    return TokenValidationResult(
                        status=TokenStatus.INVALID,
                        error_message="Token not found",
                    )

                # Check expiration
                if token_hash in self._expiry_times:
                    if datetime.now(timezone.utc) > self._expiry_times[token_hash]:
                        # Token expired, clean up (using unsafe method since lock is held)
                        self._cleanup_token_unsafe(token_hash)
                        return TokenValidationResult(
                            status=TokenStatus.EXPIRED,
                            error_message="Token has expired",
                        )

                # Get token metadata using reverse lookup
                token_id = self._token_hash_to_id.get(token_hash)
                if not token_id or token_id not in self._token_metadata:
                    return TokenValidationResult(
                        status=TokenStatus.INVALID,
                        error_message="Token metadata not found",
                    )

                meta_dict = self._token_metadata[token_id]
                token_meta = AuthToken.from_dict(meta_dict)

                # Check token status
                if token_meta.is_revoked:
                    return TokenValidationResult(
                        status=TokenStatus.REVOKED,
                        error_message="Token has been revoked",
                    )

                if token_meta.is_expired:
                    return TokenValidationResult(
                        status=TokenStatus.EXPIRED, error_message="Token has expired"
                    )

                # Update last used timestamp (using unsafe method since lock is held)
                self._update_token_usage_unsafe(token_id)

            # Release lock before external call (user_store lookup)
            # Get user information
            user_store = self._get_user_store()
            if not user_store:
                return TokenValidationResult(
                    status=TokenStatus.INVALID,
                    error_message="User store not available",
                )

            user = await user_store.get_user(user_id)

            if not user or not user.is_active:
                return TokenValidationResult(
                    status=TokenStatus.INVALID,
                    error_message="Associated user not found or inactive",
                )

            return TokenValidationResult(status=TokenStatus.VALID, user=user)

        except Exception as e:
            logger.error(f"Token validation error: {e}")
            return TokenValidationResult(
                status=TokenStatus.INVALID, error_message=f"Validation error: {str(e)}"
            )

    def _get_user_store(self):
        """Get user store (injected or from container)"""
        if self._user_store:
            return self._user_store

        # Fallback to container (for backward compatibility)
        try:
            from faultmaven.container import container

            return container.get_user_store()
        except Exception as e:
            logger.warning(f"Failed to get user store from container: {e}")
            return None

    async def revoke_token(self, token: str) -> bool:
        """Revoke a specific authentication token

        Args:
            token: Token string to revoke

        Returns:
            True if token was revoked successfully
        """
        try:
            async with self._lock:
                return self._revoke_token_unsafe(token)
        except Exception as e:
            logger.error(f"Failed to revoke token: {e}")
            return False

    def _revoke_token_unsafe(self, token: str) -> bool:
        """Internal token revocation (assumes lock is held)"""
        token_hash = self._hash_token(token)

        # Get token_id using reverse lookup
        token_id = self._token_hash_to_id.get(token_hash)
        if not token_id or token_id not in self._token_metadata:
            return False  # Token doesn't exist

        # Mark as revoked in metadata (keep metadata for status distinction)
        meta_dict = self._token_metadata[token_id]
        meta_dict["is_revoked"] = True
        self._token_metadata[token_id] = meta_dict

        # Remove from active tokens (but keep in metadata)
        user_id = self._tokens.get(token_hash)
        if token_hash in self._tokens:
            del self._tokens[token_hash]
        if token_hash in self._expiry_times:
            del self._expiry_times[token_hash]

        if user_id:
            logger.info(f"Revoked token for user {user_id}")
        return True

    async def revoke_user_tokens(self, user_id: str) -> int:
        """Revoke all tokens for a specific user

        Args:
            user_id: User ID to revoke tokens for

        Returns:
            Number of tokens revoked
        """
        try:
            async with self._lock:
                return self._revoke_user_tokens_unsafe(user_id)
        except Exception as e:
            logger.error(f"Failed to revoke user tokens: {e}")
            return 0

    def _revoke_user_tokens_unsafe(self, user_id: str) -> int:
        """Internal user token revocation (assumes lock is held)"""
        token_ids = self._user_tokens.get(user_id, [])
        revoked_count = 0

        for token_id in token_ids:
            if token_id in self._token_metadata:
                meta_dict = self._token_metadata[token_id]
                token_hash = meta_dict.get("token_hash")

                # Mark as revoked (keep metadata)
                meta_dict["is_revoked"] = True
                self._token_metadata[token_id] = meta_dict

                # Remove active token mapping
                if token_hash and token_hash in self._tokens:
                    del self._tokens[token_hash]
                if token_hash and token_hash in self._expiry_times:
                    del self._expiry_times[token_hash]

                revoked_count += 1

        # Note: We keep the user's token list for audit purposes
        # but all tokens are marked as revoked

        logger.info(f"Revoked {revoked_count} tokens for user {user_id}")
        return revoked_count

    async def cleanup_expired_tokens(self) -> int:
        """Remove expired tokens from storage

        Returns:
            Number of expired tokens cleaned up
        """
        try:
            async with self._lock:
                cleaned_count = 0
                now = datetime.now(timezone.utc)

                # Find expired tokens
                expired_hashes = [
                    token_hash
                    for token_hash, expiry in self._expiry_times.items()
                    if now > expiry
                ]

                for token_hash in expired_hashes:
                    self._cleanup_token_unsafe(token_hash)
                    cleaned_count += 1

                logger.info(f"Token cleanup completed: {cleaned_count} tokens cleaned")
                return cleaned_count

        except Exception as e:
            logger.error(f"Token cleanup failed: {e}")
            return 0

    async def get_user_tokens(self, user_id: str) -> List[AuthToken]:
        """Get all active tokens for a user

        Args:
            user_id: User ID to get tokens for

        Returns:
            List of user's authentication tokens
        """
        try:
            async with self._lock:
                token_ids = self._user_tokens.get(user_id, [])
                tokens = []

                for token_id in token_ids:
                    if token_id in self._token_metadata:
                        token_meta = AuthToken.from_dict(self._token_metadata[token_id])
                        tokens.append(token_meta)

                return tokens

        except Exception as e:
            logger.error(f"Failed to get user tokens: {e}")
            return []

    def _hash_token(self, token: str) -> str:
        """Generate SHA-256 hash of token"""
        return hashlib.sha256(token.encode()).hexdigest()

    def _update_token_usage_unsafe(self, token_id: str) -> None:
        """Update token last used timestamp (assumes lock is held)"""
        try:
            if token_id in self._token_metadata:
                meta_dict = self._token_metadata[token_id]
                meta_dict["last_used_at"] = datetime.now(timezone.utc).isoformat()
                self._token_metadata[token_id] = meta_dict
        except Exception as e:
            logger.warning(f"Failed to update token usage: {e}")

    async def _update_token_usage(self, token_id: str) -> None:
        """Public wrapper for token usage update (acquires lock)"""
        async with self._lock:
            self._update_token_usage_unsafe(token_id)

    def _cleanup_token_unsafe(self, token_hash: str) -> None:
        """Internal token cleanup (assumes lock is held)"""
        try:
            # Use reverse lookup for O(1) token_id retrieval
            token_id = self._token_hash_to_id.get(token_hash)
            if not token_id:
                # Token not found, clean up what we can
                if token_hash in self._tokens:
                    del self._tokens[token_hash]
                if token_hash in self._expiry_times:
                    del self._expiry_times[token_hash]
                return

            # Get user_id from metadata
            if token_id in self._token_metadata:
                meta_dict = self._token_metadata[token_id]
                user_id = meta_dict.get("user_id")

                # Remove from metadata
                del self._token_metadata[token_id]

                # Remove from user's token list
                if user_id and user_id in self._user_tokens:
                    if token_id in self._user_tokens[user_id]:
                        self._user_tokens[user_id].remove(token_id)
                    # Clean up empty lists
                    if not self._user_tokens[user_id]:
                        del self._user_tokens[user_id]

            # Remove from active tokens and indexes
            if token_hash in self._tokens:
                del self._tokens[token_hash]
            if token_hash in self._expiry_times:
                del self._expiry_times[token_hash]
            if token_hash in self._token_hash_to_id:
                del self._token_hash_to_id[token_hash]

        except Exception as e:
            logger.warning(f"Failed to cleanup token: {e}")

    async def _cleanup_token(self, token_hash: str) -> None:
        """Public wrapper for token cleanup (acquires lock)"""
        async with self._lock:
            self._cleanup_token_unsafe(token_hash)
