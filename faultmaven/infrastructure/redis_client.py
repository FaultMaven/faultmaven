"""
Enhanced Redis client configuration for FaultMaven.

Supports both local development and K8s cluster deployments with
proper authentication, connection pooling, and error handling.
"""

import logging
import os
from typing import Optional, Union
from urllib.parse import urlparse

import redis.asyncio as redis

logger = logging.getLogger(__name__)

class RedisClientFactory:
    """Factory for creating configured Redis clients."""
    
    @staticmethod
    def create_client(
        redis_url: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        password: Optional[str] = None,
        **kwargs
    ) -> redis.Redis:
        """
        Create a Redis client with proper configuration.
        
        Args:
            redis_url: Complete Redis URL (takes precedence)
            host: Redis host
            port: Redis port
            password: Redis password
            **kwargs: Additional Redis client parameters
            
        Returns:
            Configured Redis client
        """
        # Priority: explicit parameters > environment variables > defaults
        config = RedisClientFactory._build_config(redis_url, host, port, password)
        
        # Add connection pool settings for better performance
        pool_kwargs = {
            'max_connections': kwargs.pop('max_connections', 20),
            'socket_connect_timeout': kwargs.pop('socket_connect_timeout', 5),
            'socket_timeout': kwargs.pop('socket_timeout', 10),
        }
        
        # Note: retry_on_timeout is deprecated in redis-py 6.0.0+
        # TimeoutError is included by default in retry behavior
        
        try:
            if config['url']:
                # Use URL-based connection (includes auth)
                client = redis.from_url(
                    config['url'],
                    **pool_kwargs,
                    **kwargs
                )
                logger.info(f"Redis client created from URL: {RedisClientFactory._mask_url(config['url'])}")
            else:
                # Use parameter-based connection
                client = redis.Redis(
                    host=config['host'],
                    port=config['port'],
                    password=config['password'],
                    **pool_kwargs,
                    **kwargs
                )
                logger.info(f"Redis client created: {config['host']}:{config['port']} (auth: {'yes' if config['password'] else 'no'})")
            
            return client
            
        except Exception as e:
            logger.error(f"Failed to create Redis client: {e}")
            raise ConnectionError(f"Cannot connect to Redis: {e}")
    
    @staticmethod
    def _build_config(
        redis_url: Optional[str],
        host: Optional[str], 
        port: Optional[int],
        password: Optional[str]
    ) -> dict:
        """Build Redis configuration from various sources."""
        
        # 1. Check for explicit URL parameter
        if redis_url:
            return {'url': redis_url, 'host': None, 'port': None, 'password': None}
        
        # 2. Check environment variables for REDIS_URL
        env_url = os.getenv('REDIS_URL')
        if env_url:
            return {'url': env_url, 'host': None, 'port': None, 'password': None}
        
        # 3. Try to use configuration manager for other settings
        try:
            from ..config.configuration_manager import get_config
            config_manager = get_config()
            db_config = config_manager.get_database_config()
            
            config = {
                'url': None,
                'host': host or db_config.get('host', '192.168.0.111'),
                'port': port or db_config.get('port', 30379),
                'password': password or db_config.get('password', 'faultmaven-dev-redis-2025')
            }
            logger.debug(f"Built Redis config from ConfigurationManager: {config['host']}:{config['port']}")
        except Exception as e:
            logger.debug(f"ConfigurationManager not available, using environment variables: {e}")
            # 4. Fallback to direct environment variables
            config = {
                'url': None,
                'host': host or os.getenv('REDIS_HOST', '192.168.0.111'),
                'port': port or int(os.getenv('REDIS_PORT', '30379')),
                'password': password or os.getenv('REDIS_PASSWORD', 'faultmaven-dev-redis-2025')
            }
            logger.debug(f"Built Redis config from environment: {config['host']}:{config['port']}")
        
        return config
    
    @staticmethod
    def _mask_url(url: str) -> str:
        """Mask password in URL for logging."""
        try:
            parsed = urlparse(url)
            if parsed.password:
                masked_netloc = parsed.netloc.replace(parsed.password, '***')
                return url.replace(parsed.netloc, masked_netloc)
            return url
        except Exception:
            return url.replace('://', '://***@') if '://' in url else url
    
    @staticmethod
    async def test_connection(client: redis.Redis) -> bool:
        """
        Test Redis connection health.
        
        Args:
            client: Redis client to test
            
        Returns:
            True if connection is healthy
        """
        try:
            response = await client.ping()
            if response:
                logger.info("✅ Redis connection test successful")
                return True
            else:
                logger.error("❌ Redis ping returned False")
                return False
        except Exception as e:
            logger.error(f"❌ Redis connection test failed: {e}")
            return False


def create_redis_client(**kwargs) -> redis.Redis:
    """
    Convenience function to create a Redis client.
    
    Usage:
        # Local development
        client = create_redis_client()
        
        # K8s with environment variables
        client = create_redis_client()  # Uses REDIS_HOST, REDIS_PORT, REDIS_PASSWORD
        
        # Explicit configuration
        client = create_redis_client(
            host='192.168.0.111',
            port=30379,
            password='your-password'
        )
        
        # URL-based
        client = create_redis_client(redis_url='redis://:password@host:port/0')
    """
    return RedisClientFactory.create_client(**kwargs)


async def validate_redis_connection(client: redis.Redis) -> None:
    """
    Validate Redis connection and log results.
    
    Args:
        client: Redis client to validate
        
    Raises:
        ConnectionError: If Redis is not accessible
    """
    is_healthy = await RedisClientFactory.test_connection(client)
    if not is_healthy:
        raise ConnectionError("Redis connection validation failed")


# K8s-specific helper
def create_k8s_redis_client() -> redis.Redis:
    """
    Create Redis client specifically configured for K8s cluster.
    
    Note: This function is now redundant since create_redis_client() 
    defaults to K8s configuration. Use create_redis_client() instead.
    
    Expected environment variables:
        REDIS_HOST=192.168.0.111
        REDIS_PORT=30379
        REDIS_PASSWORD=faultmaven-dev-redis-2025
    """
    return create_redis_client()