"""
Redis implementation of ISessionStore interface.

This module provides a Redis-based session store that implements
the ISessionStore interface for consistent session management.
"""

from typing import Dict, Optional, List, Any, Union
import json
import uuid
from datetime import datetime
from faultmaven.models.interfaces import ISessionStore
from faultmaven.models.legacy import SessionContext
from faultmaven.infrastructure.redis_client import create_redis_client


class RedisSessionStore(ISessionStore):
    """Redis implementation of the ISessionStore interface"""
    
    def __init__(self):
        """Initialize Redis session store"""
        self.redis_client = None
        self.default_ttl = 1800  # 30 minutes default
        self.prefix = "session:"
        self._connection_healthy = None  # None = not yet initialized

    async def _ensure_client(self):
        """Ensure Redis client is initialized in async context"""
        if self.redis_client is None or self._connection_healthy is None:
            try:
                self.redis_client = create_redis_client()
                self._connection_healthy = True
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Failed to initialize Redis client: {e}")
                self.redis_client = None
                self._connection_healthy = False
    
    async def get(self, key: str) -> Optional[Dict]:
        """
        Get session data by key.

        Args:
            key: Session key to retrieve

        Returns:
            Session data if found, None otherwise
        """
        await self._ensure_client()
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
    
    async def set(self, key: str, value: Union[Dict, Any], ttl: Optional[int] = None) -> None:
        """
        Set session data with optional TTL.

        Args:
            key: Session key to set
            value: Session data to store
            ttl: Time to live in seconds (optional)
        """
        await self._ensure_client()
        if not self._connection_healthy or not self.redis_client:
            raise ConnectionError("Redis connection not available")

        try:
            full_key = f"{self.prefix}{key}"

            # Handle both dict and string values for compatibility
            if isinstance(value, dict):
                # Add timestamp if not present for session data
                if 'last_activity' not in value:
                    value['last_activity'] = datetime.utcnow().isoformat() + 'Z'
                serialized = json.dumps(value)
            else:
                # For non-dict values (like case_id strings), store directly as JSON
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
        await self._ensure_client()
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
        await self._ensure_client()
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
        await self._ensure_client()
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
        await self._ensure_client()
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
        await self._ensure_client()
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
        await self._ensure_client()
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

    # High-level session management methods (required by SessionService)

    async def create_session(self, user_id: Optional[str] = None) -> SessionContext:
        """Create a new session"""
        session_id = str(uuid.uuid4())
        created_at = datetime.utcnow()

        session_data = {
            'session_id': session_id,
            'user_id': user_id,
            'created_at': created_at.isoformat() + 'Z',
            'last_activity': created_at.isoformat() + 'Z',
            'data_uploads': [],
            'case_history': [],
            'metadata': {}
        }

        # Store in Redis
        await self.set(session_id, session_data)

        # Return SessionContext object
        return SessionContext(
            session_id=session_id,
            user_id=user_id,
            created_at=created_at,
            last_activity=created_at,
            data_uploads=[],
            case_history=[],
            metadata={}
        )

    async def get_session(self, session_id: str, validate: bool = True) -> Optional[SessionContext]:
        """Get session by ID"""
        session_data = await self.get(session_id)
        if not session_data:
            return None

        # Convert ISO strings back to datetime
        created_at = datetime.fromisoformat(session_data['created_at'].rstrip('Z'))
        last_activity = datetime.fromisoformat(session_data['last_activity'].rstrip('Z'))

        return SessionContext(
            session_id=session_data['session_id'],
            user_id=session_data.get('user_id'),
            created_at=created_at,
            last_activity=last_activity,
            data_uploads=session_data.get('data_uploads', []),
            case_history=session_data.get('case_history', []),
            metadata=session_data.get('metadata', {})
        )

    async def update_session(self, session_id: str, updates: Dict[str, Any]) -> bool:
        """Update session with new data"""
        session_data = await self.get(session_id)
        if not session_data:
            return False

        # Update the session data
        session_data.update(updates)
        session_data['last_activity'] = datetime.utcnow().isoformat() + 'Z'

        # Save back to Redis
        await self.set(session_id, session_data)
        return True

    async def delete_session(self, session_id: str) -> bool:
        """Delete session"""
        return await self.delete(session_id)

    async def extend_session(self, session_id: str) -> bool:
        """Extend session TTL"""
        return await self.extend_ttl(session_id)

    async def update_last_activity(self, session_id: str) -> bool:
        """Update last activity timestamp"""
        return await self.update_session(session_id, {})  # This updates last_activity automatically

    async def list_sessions(self, user_id: Optional[str] = None) -> List[SessionContext]:
        """List sessions, optionally filtered by user_id"""
        # Note: This is a simplified implementation. In production, you'd want
        # to maintain separate indexes for efficient querying
        sessions = []

        # For now, return empty list since RedisSessionStore doesn't have
        # a native way to list all sessions. This would need additional indexing.
        import logging
        logger = logging.getLogger(__name__)
        logger.warning("list_sessions is not fully implemented - would need session indexing")
        return sessions

    async def get_all_sessions(self) -> List[SessionContext]:
        """Get all sessions"""
        return await self.list_sessions()

    async def get_session_stats(self) -> Dict[str, Any]:
        """Get session statistics"""
        # Basic stats - in production you'd maintain counters
        return {
            'total_sessions': 0,  # Would need Redis counters or indexing
            'active_sessions': 0,
            'timestamp': datetime.utcnow().isoformat() + 'Z'
        }

    async def add_data_upload(self, session_id: str, data_id: str) -> bool:
        """Add data upload to session"""
        session_data = await self.get(session_id)
        if not session_data:
            return False

        if 'data_uploads' not in session_data:
            session_data['data_uploads'] = []

        session_data['data_uploads'].append(data_id)
        session_data['last_activity'] = datetime.utcnow().isoformat() + 'Z'

        await self.set(session_id, session_data)
        return True

    async def add_case_history(self, session_id: str, case_record: Dict[str, Any]) -> bool:
        """Add case history record to session"""
        session_data = await self.get(session_id)
        if not session_data:
            return False

        if 'case_history' not in session_data:
            session_data['case_history'] = []

        session_data['case_history'].append(case_record)
        session_data['last_activity'] = datetime.utcnow().isoformat() + 'Z'

        await self.set(session_id, session_data)
        return True

    async def cleanup_session_data(self, session_id: str) -> bool:
        """Clean up session data (for now, just delete the session)"""
        return await self.delete_session(session_id)