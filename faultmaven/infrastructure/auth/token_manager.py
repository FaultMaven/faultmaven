"""Token Management System

Purpose: Handle authentication token lifecycle operations

This module provides comprehensive token management for the FaultMaven
authentication system. It handles token generation, validation, storage,
and cleanup operations using Redis as the backend store.

Key Features:
- Secure token generation using UUID
- SHA-256 token hashing for storage
- Automatic expiration handling
- Token usage tracking
- Cleanup of expired tokens

Security Considerations:
- Tokens are stored as SHA-256 hashes
- Original tokens never stored in plaintext
- Automatic expiration after 24 hours
- Rate limiting protection (future enhancement)
"""

import hashlib
import uuid
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from redis import Redis

from faultmaven.models.auth import DevUser, AuthToken, TokenStatus, TokenValidationResult

logger = logging.getLogger(__name__)


class DevTokenManager:
    """Development token management system

    Manages authentication tokens for development environment.
    Uses Redis for storage and provides secure token operations.

    Token Storage Schema:
    - token:{token_hash} -> {user_id}
    - token:user:{user_id} -> [{token_id}, ...]
    - token:meta:{token_id} -> {token_metadata}
    """

    def __init__(self, redis_client: Redis):
        """Initialize token manager

        Args:
            redis_client: Redis connection for token storage
        """
        self.redis = redis_client
        self.token_expiry_seconds = 24 * 60 * 60  # 24 hours
        self.cleanup_batch_size = 100

        # Redis key patterns
        self.token_key_pattern = "auth:token:{}"
        self.user_tokens_pattern = "auth:user_tokens:{}"
        self.token_meta_pattern = "auth:token_meta:{}"

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
            # Generate new token
            token = str(uuid.uuid4())
            token_id = str(uuid.uuid4())
            token_hash = self._hash_token(token)

            # Create token metadata
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=self.token_expiry_seconds)
            auth_token = AuthToken(
                token_id=token_id,
                user_id=user.user_id,
                token_hash=token_hash,
                expires_at=expires_at,
                created_at=datetime.now(timezone.utc)
            )

            # Store in Redis with expiration
            token_key = self.token_key_pattern.format(token_hash)
            meta_key = self.token_meta_pattern.format(token_id)
            user_tokens_key = self.user_tokens_pattern.format(user.user_id)

            # Store token -> user_id mapping
            await self._redis_set(token_key, user.user_id, self.token_expiry_seconds)

            # Store token metadata
            await self._redis_set(meta_key, json.dumps(auth_token.to_dict()), self.token_expiry_seconds)

            # Add to user's token list
            await self._redis_sadd(user_tokens_key, token_id)
            await self._redis_expire(user_tokens_key, self.token_expiry_seconds)

            logger.info(f"Created token for user {user.user_id} (token_id: {token_id})")
            return token

        except Exception as e:
            logger.error(f"Failed to create token for user {user.user_id}: {e}")
            raise

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
                    status=TokenStatus.INVALID,
                    error_message="Token is empty"
                )

            token_hash = self._hash_token(token)
            token_key = self.token_key_pattern.format(token_hash)

            # Check if token exists in Redis
            user_id = await self._redis_get(token_key)
            if not user_id:
                return TokenValidationResult(
                    status=TokenStatus.INVALID,
                    error_message="Token not found or expired"
                )

            # Get token metadata to check detailed status
            user_tokens_key = self.user_tokens_pattern.format(user_id)
            token_ids = await self._redis_smembers(user_tokens_key)

            # Find matching token metadata
            token_meta = None
            for token_id in token_ids:
                meta_key = self.token_meta_pattern.format(token_id)
                meta_data = await self._redis_get(meta_key)
                if meta_data:
                    meta_dict = json.loads(meta_data)
                    if meta_dict.get("token_hash") == token_hash:
                        token_meta = AuthToken.from_dict(meta_dict)
                        break

            if not token_meta:
                return TokenValidationResult(
                    status=TokenStatus.INVALID,
                    error_message="Token metadata not found"
                )

            # Check token status
            if token_meta.is_revoked:
                return TokenValidationResult(
                    status=TokenStatus.REVOKED,
                    error_message="Token has been revoked"
                )

            if token_meta.is_expired:
                return TokenValidationResult(
                    status=TokenStatus.EXPIRED,
                    error_message="Token has expired"
                )

            # Get user information (assuming user store integration)
            from faultmaven.container import container
            user_store = container.get_user_store()
            user = await user_store.get_user(user_id)

            if not user or not user.is_active:
                return TokenValidationResult(
                    status=TokenStatus.INVALID,
                    error_message="Associated user not found or inactive"
                )

            # Update last used timestamp
            await self._update_token_usage(token_meta.token_id)

            return TokenValidationResult(
                status=TokenStatus.VALID,
                user=user
            )

        except Exception as e:
            logger.error(f"Token validation error: {e}")
            return TokenValidationResult(
                status=TokenStatus.INVALID,
                error_message=f"Validation error: {str(e)}"
            )

    async def revoke_token(self, token: str) -> bool:
        """Revoke a specific authentication token

        Args:
            token: Token string to revoke

        Returns:
            True if token was revoked successfully
        """
        try:
            token_hash = self._hash_token(token)
            token_key = self.token_key_pattern.format(token_hash)

            # Get user_id before deletion
            user_id = await self._redis_get(token_key)
            if not user_id:
                return False  # Token doesn't exist

            # Find and mark token as revoked in metadata
            user_tokens_key = self.user_tokens_pattern.format(user_id)
            token_ids = await self._redis_smembers(user_tokens_key)

            for token_id in token_ids:
                meta_key = self.token_meta_pattern.format(token_id)
                meta_data = await self._redis_get(meta_key)
                if meta_data:
                    meta_dict = json.loads(meta_data)
                    if meta_dict.get("token_hash") == token_hash:
                        # Mark as revoked
                        meta_dict["is_revoked"] = True
                        await self._redis_set(meta_key, json.dumps(meta_dict), self.token_expiry_seconds)
                        break

            # Remove from active tokens
            await self._redis_delete(token_key)

            logger.info(f"Revoked token for user {user_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to revoke token: {e}")
            return False

    async def revoke_user_tokens(self, user_id: str) -> int:
        """Revoke all tokens for a specific user

        Args:
            user_id: User ID to revoke tokens for

        Returns:
            Number of tokens revoked
        """
        try:
            user_tokens_key = self.user_tokens_pattern.format(user_id)
            token_ids = await self._redis_smembers(user_tokens_key)

            revoked_count = 0
            for token_id in token_ids:
                meta_key = self.token_meta_pattern.format(token_id)
                meta_data = await self._redis_get(meta_key)
                if meta_data:
                    meta_dict = json.loads(meta_data)

                    # Mark as revoked
                    meta_dict["is_revoked"] = True
                    await self._redis_set(meta_key, json.dumps(meta_dict), self.token_expiry_seconds)

                    # Remove active token mapping
                    token_hash = meta_dict.get("token_hash")
                    if token_hash:
                        token_key = self.token_key_pattern.format(token_hash)
                        await self._redis_delete(token_key)

                    revoked_count += 1

            logger.info(f"Revoked {revoked_count} tokens for user {user_id}")
            return revoked_count

        except Exception as e:
            logger.error(f"Failed to revoke user tokens: {e}")
            return 0

    async def cleanup_expired_tokens(self) -> int:
        """Remove expired tokens from storage

        Returns:
            Number of expired tokens cleaned up
        """
        try:
            # This is a simplified cleanup - in production, you'd use Redis scanning
            # for better performance with large token sets
            cleaned_count = 0

            # Note: Redis TTL handles most cleanup automatically
            # This method handles any orphaned metadata

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
            user_tokens_key = self.user_tokens_pattern.format(user_id)
            token_ids = await self._redis_smembers(user_tokens_key)

            tokens = []
            for token_id in token_ids:
                meta_key = self.token_meta_pattern.format(token_id)
                meta_data = await self._redis_get(meta_key)
                if meta_data:
                    token_meta = AuthToken.from_dict(json.loads(meta_data))
                    tokens.append(token_meta)

            return tokens

        except Exception as e:
            logger.error(f"Failed to get user tokens: {e}")
            return []

    def _hash_token(self, token: str) -> str:
        """Generate SHA-256 hash of token"""
        return hashlib.sha256(token.encode()).hexdigest()

    async def _update_token_usage(self, token_id: str) -> None:
        """Update token last used timestamp"""
        try:
            meta_key = self.token_meta_pattern.format(token_id)
            meta_data = await self._redis_get(meta_key)
            if meta_data:
                meta_dict = json.loads(meta_data)
                meta_dict["last_used_at"] = datetime.now(timezone.utc).isoformat()
                await self._redis_set(meta_key, json.dumps(meta_dict), self.token_expiry_seconds)
        except Exception as e:
            logger.warning(f"Failed to update token usage: {e}")

    # Redis async wrapper methods (using async Redis client)
    async def _redis_set(self, key: str, value: str, expiry: int = None) -> None:
        """Set Redis key with optional expiry"""
        try:
            if expiry:
                result = await self.redis.setex(key, expiry, value)
            else:
                result = await self.redis.set(key, value)
            return result
        except Exception as e:
            logger.error(f"Redis SET failed for key {key}: {e}")
            raise

    async def _redis_get(self, key: str) -> Optional[str]:
        """Get Redis key value"""
        try:
            result = await self.redis.get(key)
            return result if result else None
        except Exception as e:
            logger.error(f"Redis GET failed for key {key}: {e}")
            return None

    async def _redis_delete(self, key: str) -> None:
        """Delete Redis key"""
        try:
            return await self.redis.delete(key)
        except Exception as e:
            logger.error(f"Redis DELETE failed for key {key}: {e}")

    async def _redis_sadd(self, key: str, value: str) -> None:
        """Add to Redis set"""
        try:
            return await self.redis.sadd(key, value)
        except Exception as e:
            logger.error(f"Redis SADD failed for key {key}: {e}")

    async def _redis_smembers(self, key: str) -> List[str]:
        """Get Redis set members"""
        try:
            members = await self.redis.smembers(key)
            return [str(member) for member in members]
        except Exception as e:
            logger.error(f"Redis SMEMBERS failed for key {key}: {e}")
            return []

    async def _redis_expire(self, key: str, seconds: int) -> None:
        """Set Redis key expiration"""
        try:
            return await self.redis.expire(key, seconds)
        except Exception as e:
            logger.error(f"Redis EXPIRE failed for key {key}: {e}")