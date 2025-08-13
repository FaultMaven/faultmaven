"""
Redis implementation of ISessionStore interface.

This module provides a Redis-based session store that implements
the ISessionStore interface for consistent session management.
"""

from typing import Dict, Optional
import json
from datetime import datetime
from faultmaven.models.interfaces import ISessionStore
from faultmaven.infrastructure.persistence.redis import RedisClient


class RedisSessionStore(ISessionStore):
    """Redis implementation of the ISessionStore interface"""
    
    def __init__(self):
        """Initialize Redis session store"""
        self.redis_client = RedisClient()
        self.default_ttl = 1800  # 30 minutes default
        self.prefix = "session:"
    
    async def get(self, key: str) -> Optional[Dict]:
        """
        Get session data by key.
        
        Args:
            key: Session key to retrieve
            
        Returns:
            Session data if found, None otherwise
        """
        full_key = f"{self.prefix}{key}"
        data = await self.redis_client.get(full_key)
        
        if data:
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                return None
        return None
    
    async def set(self, key: str, value: Dict, ttl: Optional[int] = None) -> None:
        """
        Set session data with optional TTL.
        
        Args:
            key: Session key to set
            value: Session data to store
            ttl: Time to live in seconds (optional)
        """
        full_key = f"{self.prefix}{key}"
        
        # Add timestamp if not present
        if 'last_activity' not in value:
            value['last_activity'] = datetime.utcnow().isoformat()
        
        serialized = json.dumps(value)
        ttl = ttl if ttl is not None else self.default_ttl
        
        await self.redis_client.set(full_key, serialized, ex=ttl)
    
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