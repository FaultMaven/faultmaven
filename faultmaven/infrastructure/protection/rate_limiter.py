"""
Redis-backed rate limiting implementation

Provides sliding window rate limiting with multiple bucket types,
progressive penalties, and graceful degradation.
"""

import asyncio
import logging
import time
import json
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List, Tuple
from dataclasses import asdict

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from ...models.protection import (
    RateLimitConfig,
    RateLimitState,
    RateLimitResult,
    LimitType,
    RateLimitError
)


class RedisRateLimiter:
    """
    Redis-backed sliding window rate limiter
    
    Features:
    - Multiple limit types (global, per-session, per-endpoint)
    - Sliding window algorithm for smooth rate limiting
    - Progressive penalties for repeated violations
    - Graceful degradation when Redis is unavailable
    - Security features (jitter, constant-time operations)
    """
    
    def __init__(
        self,
        redis_url: str,
        key_prefix: str = "fm:rl",
        fallback_enabled: bool = True
    ):
        self.redis_url = redis_url
        self.key_prefix = key_prefix
        self.fallback_enabled = fallback_enabled
        self.logger = logging.getLogger(__name__)
        
        # Redis connection
        self._redis: Optional[aioredis.Redis] = None
        self._redis_healthy = True
        
        # In-memory fallback for when Redis is unavailable
        self._fallback_store: Dict[str, RateLimitState] = {}
        self._fallback_cleanup_interval = 60  # seconds
        self._last_fallback_cleanup = time.time()
        
        # Rate limit configurations
        self._configs: Dict[str, RateLimitConfig] = {}
        
        # Penalty tracking
        self._penalty_multipliers = {
            "first_violation": 2.0,
            "second_violation": 4.0,
            "third_violation": 8.0,
            "persistent_violation": 16.0
        }
    
    async def initialize(self) -> None:
        """Initialize Redis connection"""
        try:
            self._redis = aioredis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                retry_on_timeout=True,
                health_check_interval=30
            )
            
            # Test connection
            await self._redis.ping()
            self._redis_healthy = True
            self.logger.info("Redis rate limiter initialized successfully")
            
        except Exception as e:
            self.logger.error(f"Failed to initialize Redis rate limiter: {e}")
            self._redis_healthy = False
            if not self.fallback_enabled:
                raise
    
    async def close(self) -> None:
        """Close Redis connection"""
        if self._redis:
            await self._redis.close()
    
    def configure_limits(self, limits: Dict[str, RateLimitConfig]) -> None:
        """Configure rate limits"""
        self._configs = limits.copy()
        self.logger.info(f"Configured {len(limits)} rate limit types")
    
    async def check_rate_limit(
        self,
        key: str,
        limit_type: LimitType,
        identifier: str = ""
    ) -> RateLimitResult:
        """
        Check if request is within rate limits
        
        Args:
            key: Unique key for the rate limit bucket
            limit_type: Type of rate limit to check
            identifier: Additional identifier for logging
            
        Returns:
            RateLimitResult with decision and metadata
        """
        start_time = time.time()
        
        try:
            # Get configuration for this limit type
            config = self._configs.get(limit_type.value)
            if not config or not config.enabled:
                return RateLimitResult(
                    allowed=True,
                    limit_type=limit_type,
                    current_count=0,
                    limit=0
                )
            
            # Create rate limit key
            rate_limit_key = f"{self.key_prefix}:{limit_type.value}:{key}"
            
            # Check rate limit
            if self._redis_healthy and self._redis:
                result = await self._check_redis_rate_limit(
                    rate_limit_key, config, limit_type
                )
            else:
                result = await self._check_fallback_rate_limit(
                    rate_limit_key, config, limit_type
                )
            
            # Log rate limit check
            duration = time.time() - start_time
            self.logger.debug(
                f"Rate limit check: key={key}, type={limit_type.value}, "
                f"allowed={result.allowed}, count={result.current_count}/"
                f"{result.limit}, duration={duration:.3f}s"
            )
            
            return result
            
        except Exception as e:
            self.logger.error(f"Rate limit check failed: {e}")
            
            # Fail open if configured
            if self.fallback_enabled:
                return RateLimitResult(
                    allowed=True,
                    limit_type=limit_type,
                    current_count=0,
                    limit=0
                )
            else:
                raise RateLimitError(
                    retry_after=60,
                    limit_type=limit_type.value,
                    current_count=0,
                    limit=0
                )
    
    async def _check_redis_rate_limit(
        self,
        key: str,
        config: RateLimitConfig,
        limit_type: LimitType
    ) -> RateLimitResult:
        """Check rate limit using Redis sliding window"""
        
        current_time = int(time.time())
        window_start = current_time - config.window
        
        # Lua script for atomic sliding window rate limiting
        lua_script = """
        local key = KEYS[1]
        local window_start = tonumber(ARGV[1])
        local current_time = tonumber(ARGV[2])
        local limit = tonumber(ARGV[3])
        local ttl = tonumber(ARGV[4])
        
        -- Remove expired entries
        redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)
        
        -- Count current entries
        local current_count = redis.call('ZCARD', key)
        
        -- Check if limit exceeded
        if current_count >= limit then
            return {current_count, limit, 0}  -- blocked
        end
        
        -- Add current request
        redis.call('ZADD', key, current_time, current_time)
        redis.call('EXPIRE', key, ttl)
        
        return {current_count + 1, limit, 1}  -- allowed
        """
        
        try:
            result = await self._redis.eval(
                lua_script,
                1,  # number of keys
                key,
                window_start,
                current_time,
                config.requests,
                config.window + 60  # TTL with buffer
            )
            
            current_count, limit, allowed = result
            
            if not allowed:
                # Calculate retry after with jitter and penalties
                retry_after = self._calculate_retry_after(key, config.window)
                
                return RateLimitResult(
                    allowed=False,
                    limit_type=limit_type,
                    current_count=current_count,
                    limit=limit,
                    retry_after=retry_after,
                    reset_time=datetime.fromtimestamp(current_time + config.window, tz=timezone.utc)
                )
            
            return RateLimitResult(
                allowed=True,
                limit_type=limit_type,
                current_count=current_count,
                limit=limit,
                reset_time=datetime.fromtimestamp(current_time + config.window, tz=timezone.utc)
            )
            
        except RedisError as e:
            self.logger.warning(f"Redis rate limit check failed, falling back: {e}")
            self._redis_healthy = False
            
            # Fall back to in-memory check
            return await self._check_fallback_rate_limit(key, config, limit_type)
    
    async def _check_fallback_rate_limit(
        self,
        key: str,
        config: RateLimitConfig,
        limit_type: LimitType
    ) -> RateLimitResult:
        """Fallback in-memory rate limiting"""
        
        current_time = time.time()
        
        # Clean up old entries periodically
        if current_time - self._last_fallback_cleanup > self._fallback_cleanup_interval:
            await self._cleanup_fallback_store()
            self._last_fallback_cleanup = current_time
        
        # Get or create rate limit state
        if key not in self._fallback_store:
            self._fallback_store[key] = RateLimitState(
                key=key,
                limit_type=limit_type,
                current_count=0,
                limit=config.requests,
                window=config.window,
                reset_time=datetime.fromtimestamp(current_time + config.window, tz=timezone.utc)
            )
        
        state = self._fallback_store[key]
        
        # Check if window has expired
        if current_time >= state.reset_time.timestamp():
            state.current_count = 0
            state.reset_time = datetime.fromtimestamp(current_time + config.window, tz=timezone.utc)
        
        # Check limit
        if state.current_count >= state.limit:
            retry_after = int(state.reset_time.timestamp() - current_time)
            
            return RateLimitResult(
                allowed=False,
                limit_type=limit_type,
                current_count=state.current_count,
                limit=state.limit,
                retry_after=retry_after,
                reset_time=state.reset_time
            )
        
        # Increment counter
        state.current_count += 1
        
        return RateLimitResult(
            allowed=True,
            limit_type=limit_type,
            current_count=state.current_count,
            limit=state.limit,
            reset_time=state.reset_time
        )
    
    async def _cleanup_fallback_store(self) -> None:
        """Clean up expired entries from fallback store"""
        current_time = time.time()
        expired_keys = []
        
        for key, state in self._fallback_store.items():
            if current_time >= state.reset_time.timestamp():
                expired_keys.append(key)
        
        for key in expired_keys:
            del self._fallback_store[key]
        
        if expired_keys:
            self.logger.debug(f"Cleaned up {len(expired_keys)} expired fallback entries")
    
    def _calculate_retry_after(self, key: str, base_window: int) -> int:
        """Calculate retry after time with penalties and jitter"""
        
        # Get violation count for progressive penalties
        violation_key = f"{key}:violations"
        base_retry = base_window
        
        try:
            if self._redis and self._redis_healthy:
                # Use Redis to track violations
                violation_count = asyncio.create_task(
                    self._redis.incr(violation_key)
                )
                asyncio.create_task(
                    self._redis.expire(violation_key, base_window * 4)
                )
                violation_count = violation_count.result() if violation_count.done() else 1
            else:
                # Use in-memory tracking
                violation_count = 1
        except:
            violation_count = 1
        
        # Apply progressive penalties
        if violation_count <= 1:
            multiplier = 1.0
        elif violation_count == 2:
            multiplier = self._penalty_multipliers["first_violation"]
        elif violation_count == 3:
            multiplier = self._penalty_multipliers["second_violation"]
        elif violation_count == 4:
            multiplier = self._penalty_multipliers["third_violation"]
        else:
            multiplier = self._penalty_multipliers["persistent_violation"]
        
        retry_after = int(base_retry * multiplier)
        
        # Add jitter to prevent thundering herd
        jitter = random.uniform(0, retry_after * 0.1)
        retry_after = int(retry_after + jitter)
        
        return min(retry_after, 300)  # Cap at 5 minutes
    
    async def get_rate_limit_status(self, key: str, limit_type: LimitType) -> Optional[RateLimitState]:
        """Get current rate limit status without incrementing"""
        
        config = self._configs.get(limit_type.value)
        if not config:
            return None
        
        rate_limit_key = f"{self.key_prefix}:{limit_type.value}:{key}"
        
        try:
            if self._redis and self._redis_healthy:
                current_time = int(time.time())
                window_start = current_time - config.window
                
                # Get current count
                await self._redis.zremrangebyscore(rate_limit_key, '-inf', window_start)
                current_count = await self._redis.zcard(rate_limit_key)
                
                return RateLimitState(
                    key=key,
                    limit_type=limit_type,
                    current_count=current_count,
                    limit=config.requests,
                    window=config.window,
                    reset_time=datetime.fromtimestamp(current_time + config.window, tz=timezone.utc)
                )
            else:
                # Use fallback store
                if rate_limit_key in self._fallback_store:
                    return self._fallback_store[rate_limit_key]
                
        except Exception as e:
            self.logger.error(f"Failed to get rate limit status: {e}")
        
        return None
    
    async def reset_rate_limit(self, key: str, limit_type: LimitType) -> bool:
        """Reset rate limit for a specific key (admin function)"""
        
        rate_limit_key = f"{self.key_prefix}:{limit_type.value}:{key}"
        violation_key = f"{rate_limit_key}:violations"
        
        try:
            if self._redis and self._redis_healthy:
                deleted = await self._redis.delete(rate_limit_key, violation_key)
                self.logger.info(f"Reset rate limit for {key}:{limit_type.value} (deleted {deleted} keys)")
                return deleted > 0
            else:
                # Reset fallback store
                if rate_limit_key in self._fallback_store:
                    del self._fallback_store[rate_limit_key]
                    return True
                
        except Exception as e:
            self.logger.error(f"Failed to reset rate limit: {e}")
        
        return False
    
    async def health_check(self) -> Dict[str, any]:
        """Perform health check and return status"""
        
        status = {
            "redis_healthy": self._redis_healthy,
            "fallback_enabled": self.fallback_enabled,
            "fallback_entries": len(self._fallback_store),
            "configured_limits": len(self._configs)
        }
        
        try:
            if self._redis:
                ping_result = await self._redis.ping()
                status["redis_ping"] = ping_result
                self._redis_healthy = True
        except Exception as e:
            status["redis_error"] = str(e)
            self._redis_healthy = False
        
        return status