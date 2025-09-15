"""
Redis implementation of ISessionStore interface.

This module provides a Redis-based session store that implements
the ISessionStore interface for consistent session management.
"""

from typing import Dict, Optional
import json
from datetime import datetime
from faultmaven.models.interfaces import ISessionStore
from faultmaven.infrastructure.redis_client import create_redis_client


class RedisSessionStore(ISessionStore):
    """Redis implementation of the ISessionStore interface"""
    
    def __init__(self):
        """Initialize Redis session store"""
        try:
            self.redis_client = create_redis_client()
            self.default_ttl = 1800  # 30 minutes default
            self.prefix = "session:"
            self._connection_healthy = True
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Failed to initialize Redis client: {e}")
            self.redis_client = None
            self.default_ttl = 1800
            self.prefix = "session:"
            self._connection_healthy = False
    
    async def get(self, key: str) -> Optional[Dict]:
        """
        Get session data by key.
        
        Args:
            key: Session key to retrieve
            
        Returns:
            Session data if found, None otherwise
        """
        if not self._connection_healthy or not self.redis_client:
            raise ConnectionError("Redis connection not available")
        
        try:
            full_key = f"{self.prefix}{key}"
            data = await self.redis_client.get(full_key)
            
            if data:
                try:
                    return json.loads(data)
                except json.JSONDecodeError:
                    return None
            return None
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Redis get operation failed for key {key}: {e}")
            self._connection_healthy = False
            raise ConnectionError(f"Redis operation failed: {e}")
    
    async def set(self, key: str, value: Dict, ttl: Optional[int] = None) -> None:
        """
        Set session data with optional TTL.
        
        Args:
            key: Session key to set
            value: Session data to store
            ttl: Time to live in seconds (optional)
        """
        if not self._connection_healthy or not self.redis_client:
            raise ConnectionError("Redis connection not available")
        
        try:
            full_key = f"{self.prefix}{key}"
            
            # Add timestamp if not present
            if 'last_activity' not in value:
                value['last_activity'] = datetime.utcnow().isoformat() + 'Z'
            
            serialized = json.dumps(value)
            ttl = ttl if ttl is not None else self.default_ttl
            
            await self.redis_client.set(full_key, serialized, ex=ttl)
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Redis set operation failed for key {key}: {e}")
            self._connection_healthy = False
            raise ConnectionError(f"Redis operation failed: {e}")
    
    async def delete(self, key: str) -> bool:
        """
        Delete session by key.
        
        Args:
            key: Session key to delete
            
        Returns:
            True if deleted, False if not found
        """
        full_key = f"{self.prefix}{key}"
        result = await self.redis_client.delete(full_key)
        return result > 0
    
    async def exists(self, key: str) -> bool:
        """
        Check if session exists.
        
        Args:
            key: Session key to check
            
        Returns:
            True if exists, False otherwise
        """
        full_key = f"{self.prefix}{key}"
        return await self.redis_client.exists(full_key) > 0
    
    async def extend_ttl(self, key: str, ttl: Optional[int] = None) -> bool:
        """
        Extend session TTL.
        
        Args:
            key: Session key to extend
            ttl: New TTL in seconds
            
        Returns:
            True if extended, False if not found
        """
        full_key = f"{self.prefix}{key}"
        ttl = ttl if ttl is not None else self.default_ttl
        return await self.redis_client.expire(full_key, ttl)
    
    async def find_by_user_and_client(self, user_id: str, client_id: str) -> Optional[str]:
        """
        Find session ID by user_id and client_id combination.
        
        Args:
            user_id: User identifier to search for
            client_id: Client/device identifier to search for
            
        Returns:
            Session ID if found, None if no matching session exists
        """
        if not self._connection_healthy or not self.redis_client:
            raise ConnectionError("Redis connection not available")
        
        try:
            client_index_key = f"client_index:{user_id}:{client_id}"
            session_id = await self.redis_client.get(client_index_key)
            
            if session_id:
                # Decode bytes to string if needed
                if isinstance(session_id, bytes):
                    session_id = session_id.decode('utf-8')
                return session_id
            return None
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Redis find_by_user_and_client operation failed for user {user_id}, client {client_id}: {e}")
            self._connection_healthy = False
            raise ConnectionError(f"Redis operation failed: {e}")
    
    async def index_session_by_client(self, user_id: str, client_id: str, session_id: str, ttl: int) -> None:
        """
        Create or update index entry for (user_id, client_id) -> session_id mapping.
        
        Args:
            user_id: User identifier for the index key
            client_id: Client/device identifier for the index key
            session_id: Session ID to index
            ttl: Time to live in seconds
        """
        if not self._connection_healthy or not self.redis_client:
            raise ConnectionError("Redis connection not available")
        
        try:
            client_index_key = f"client_index:{user_id}:{client_id}"
            
            # Use Redis pipeline for atomic operation
            pipe = self.redis_client.pipeline()
            pipe.set(client_index_key, session_id, ex=ttl)
            await pipe.execute()
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Redis index_session_by_client operation failed for user {user_id}, client {client_id}, session {session_id}: {e}")
            self._connection_healthy = False
            raise ConnectionError(f"Redis operation failed: {e}")
    
    async def remove_client_index(self, user_id: str, client_id: str) -> None:
        """
        Remove client index entry for cleanup.
        
        Args:
            user_id: User identifier for the index key
            client_id: Client/device identifier for the index key
        """
        if not self._connection_healthy or not self.redis_client:
            raise ConnectionError("Redis connection not available")
        
        try:
            client_index_key = f"client_index:{user_id}:{client_id}"
            await self.redis_client.delete(client_index_key)
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Redis remove_client_index operation failed for user {user_id}, client {client_id}: {e}")
            # Don't mark connection as unhealthy for cleanup operations
            # Just log and continue