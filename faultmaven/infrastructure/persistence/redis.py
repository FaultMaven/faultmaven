"""
Redis Client with BaseExternalClient integration.

Provides a Redis client that inherits from BaseExternalClient for unified
logging, retry logic, circuit breaker patterns, and comprehensive error handling.
"""

import redis.asyncio as redis
from typing import Any, Dict, Optional, Union
from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.infrastructure.redis_client import RedisClientFactory


class RedisClient(BaseExternalClient):
    """
    Redis client with BaseExternalClient integration.
    
    This class wraps the Redis client with unified infrastructure logging,
    retry logic, and circuit breaker protection for all Redis operations.
    """
    
    def __init__(
        self,
        redis_url: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        password: Optional[str] = None,
        **kwargs
    ):
        """
        Initialize Redis client with BaseExternalClient integration.
        
        Args:
            redis_url: Complete Redis URL (takes precedence)
            host: Redis host
            port: Redis port
            password: Redis password
            **kwargs: Additional Redis client parameters
        """
        # Initialize BaseExternalClient
        super().__init__(
            client_name="redis_client",
            service_name="Redis",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=5,  # Standard threshold for database
            circuit_breaker_timeout=60    # Standard timeout for database recovery
        )
        
        # Create the underlying Redis client
        self._client = RedisClientFactory.create_client(
            redis_url=redis_url,
            host=host,
            port=port,
            password=password,
            **kwargs
        )
        
        # Log initialization
        self.logger.log_event(
            event_type="system",
            event_name="redis_client_initialized",
            severity="info",
            data={
                "redis_host": host or "from_url",
                "redis_port": port or "from_url"
            }
        )
    
    async def get(self, key: str) -> Optional[str]:
        """Get value from Redis with external call wrapping."""
        async def _get_wrapper():
            return await self._client.get(key)
        
        return await self.call_external(
            operation_name="get",
            call_func=_get_wrapper,
            timeout=5.0,
            retries=2,
            retry_delay=1.0
        )
    
    async def set(
        self,
        key: str,
        value: Union[str, bytes],
        ex: Optional[int] = None,
        nx: bool = False
    ) -> bool:
        """Set value in Redis with external call wrapping."""
        async def _set_wrapper():
            return await self._client.set(key, value, ex=ex, nx=nx)
        
        return await self.call_external(
            operation_name="set",
            call_func=_set_wrapper,
            timeout=5.0,
            retries=2,
            retry_delay=1.0
        )
    
    async def delete(self, *keys: str) -> int:
        """Delete keys from Redis with external call wrapping."""
        async def _delete_wrapper():
            return await self._client.delete(*keys)
        
        return await self.call_external(
            operation_name="delete",
            call_func=_delete_wrapper,
            timeout=5.0,
            retries=2,
            retry_delay=1.0
        )
    
    async def exists(self, *keys: str) -> int:
        """Check if keys exist in Redis with external call wrapping."""
        async def _exists_wrapper():
            return await self._client.exists(*keys)
        
        return await self.call_external(
            operation_name="exists",
            call_func=_exists_wrapper,
            timeout=3.0,
            retries=1,
            retry_delay=0.5
        )
    
    async def expire(self, key: str, time: int) -> bool:
        """Set expiration time for key with external call wrapping."""
        async def _expire_wrapper():
            return await self._client.expire(key, time)
        
        return await self.call_external(
            operation_name="expire",
            call_func=_expire_wrapper,
            timeout=3.0,
            retries=1,
            retry_delay=0.5
        )
    
    async def ttl(self, key: str) -> int:
        """Get time to live for key with external call wrapping."""
        async def _ttl_wrapper():
            return await self._client.ttl(key)
        
        return await self.call_external(
            operation_name="ttl",
            call_func=_ttl_wrapper,
            timeout=3.0,
            retries=1,
            retry_delay=0.5
        )
    
    async def ping(self) -> bool:
        """Ping Redis server with external call wrapping."""
        async def _ping_wrapper():
            return await self._client.ping()
        
        response = await self.call_external(
            operation_name="ping",
            call_func=_ping_wrapper,
            timeout=5.0,
            retries=1,
            retry_delay=1.0
        )
        return response is True
    
    async def keys(self, pattern: str = "*") -> list:
        """Get keys matching pattern with external call wrapping."""
        async def _keys_wrapper():
            return await self._client.keys(pattern)
        
        return await self.call_external(
            operation_name="keys",
            call_func=_keys_wrapper,
            timeout=10.0,
            retries=1,
            retry_delay=1.0
        )
    
    async def hget(self, name: str, key: str) -> Optional[str]:
        """Get field from hash with external call wrapping."""
        async def _hget_wrapper():
            return await self._client.hget(name, key)
        
        return await self.call_external(
            operation_name="hget",
            call_func=_hget_wrapper,
            timeout=5.0,
            retries=2,
            retry_delay=1.0
        )
    
    async def hset(self, name: str, key: str, value: Union[str, bytes]) -> int:
        """Set field in hash with external call wrapping."""
        async def _hset_wrapper():
            return await self._client.hset(name, key, value)
        
        return await self.call_external(
            operation_name="hset",
            call_func=_hset_wrapper,
            timeout=5.0,
            retries=2,
            retry_delay=1.0
        )
    
    async def hdel(self, name: str, *keys: str) -> int:
        """Delete fields from hash with external call wrapping."""
        async def _hdel_wrapper():
            return await self._client.hdel(name, *keys)
        
        return await self.call_external(
            operation_name="hdel",
            call_func=_hdel_wrapper,
            timeout=5.0,
            retries=2,
            retry_delay=1.0
        )
    
    async def hgetall(self, name: str) -> Dict[str, str]:
        """Get all fields and values from hash with external call wrapping."""
        async def _hgetall_wrapper():
            return await self._client.hgetall(name)
        
        return await self.call_external(
            operation_name="hgetall",
            call_func=_hgetall_wrapper,
            timeout=10.0,
            retries=1,
            retry_delay=1.0
        )
    
    async def health_check(self) -> Dict[str, Any]:
        """
        Perform comprehensive health check for Redis client.
        
        Returns:
            Dictionary containing health status and metrics
        """
        base_health = await super().health_check()
        
        # Add Redis-specific health data
        try:
            ping_result = await self.ping()
            redis_health = {
                "ping_successful": ping_result,
                "connection_pool": {
                    "created_connections": getattr(self._client.connection_pool, "created_connections", "unknown"),
                    "available_connections": getattr(self._client.connection_pool, "_available_connections", "unknown"),
                    "in_use_connections": getattr(self._client.connection_pool, "_in_use_connections", "unknown")
                }
            }
            
            base_health.update({
                "redis_specific": redis_health,
                "status": "healthy" if ping_result else "degraded"
            })
            
        except Exception as e:
            base_health.update({
                "redis_specific": {"error": str(e)},
                "status": "unhealthy"
            })
        
        return base_health
    
    async def close(self) -> None:
        """Close Redis connection with logging."""
        try:
            async def _close_wrapper():
                await self._client.close()
            
            await self.call_external(
                operation_name="close",
                call_func=_close_wrapper,
                timeout=5.0
            )
            
            self.logger.log_event(
                event_type="system",
                event_name="redis_client_closed",
                severity="info"
            )
        except Exception as e:
            self.logger.error(f"Error closing Redis client: {e}")
            raise
    
    def __getattr__(self, name: str) -> Any:
        """
        Delegate unknown attributes to the underlying Redis client.
        
        This allows access to Redis methods not explicitly wrapped while
        still maintaining the BaseExternalClient functionality for the
        most common operations.
        """
        if hasattr(self._client, name):
            return getattr(self._client, name)
        raise AttributeError(f"RedisClient has no attribute '{name}'")