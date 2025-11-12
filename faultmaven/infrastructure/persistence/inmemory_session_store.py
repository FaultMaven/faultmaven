"""
In-memory implementation of ISessionStore interface.

This module provides a RAM-based session store for development and testing.
Data is stored in Python dictionaries and lost on application restart.
"""

from typing import Dict, Optional
from datetime import datetime, timezone, timedelta
import asyncio
from faultmaven.models.interfaces import ISessionStore


class InMemorySessionStore(ISessionStore):
    """In-memory implementation of ISessionStore using Python dictionaries"""

    def __init__(self):
        """Initialize in-memory session store with Python dicts"""
        self._sessions: Dict[str, Dict] = {}  # session_id -> session_data
        self._client_index: Dict[str, str] = {}  # "user_id:client_id" -> session_id
        self._ttls: Dict[str, datetime] = {}  # session_id -> expiration_time
        self._lock = asyncio.Lock()  # For thread-safe operations

    async def get(self, key: str) -> Optional[Dict]:
        """
        Get session data by key.

        Args:
            key: Session identifier

        Returns:
            Session data if found and not expired, None otherwise
        """
        async with self._lock:
            # Check expiration
            if key in self._ttls:
                if datetime.now(timezone.utc) > self._ttls[key]:
                    # Session expired, clean up
                    await self._cleanup_session(key)
                    return None

            return self._sessions.get(key)

    async def set(self, key: str, value: Dict, ttl: Optional[int] = None) -> None:
        """
        Store session data with optional TTL.

        Args:
            key: Session identifier
            value: Session data dictionary
            ttl: Time to live in seconds (default 1800 = 30 minutes)
        """
        async with self._lock:
            ttl = ttl or 1800  # Default 30 minutes

            # Add last_activity timestamp if not present
            if 'last_activity' not in value:
                value['last_activity'] = datetime.now(timezone.utc).isoformat()

            self._sessions[key] = value
            self._ttls[key] = datetime.now(timezone.utc) + timedelta(seconds=ttl)

    async def delete(self, key: str) -> bool:
        """
        Remove session from storage.

        Args:
            key: Session identifier

        Returns:
            True if session was deleted, False if not found
        """
        async with self._lock:
            if key not in self._sessions:
                return False

            await self._cleanup_session(key)
            return True

    async def exists(self, key: str) -> bool:
        """
        Check if session exists without retrieving data.

        Args:
            key: Session identifier

        Returns:
            True if session exists and hasn't expired, False otherwise
        """
        async with self._lock:
            # Check expiration
            if key in self._ttls:
                if datetime.now(timezone.utc) > self._ttls[key]:
                    await self._cleanup_session(key)
                    return False

            return key in self._sessions

    async def extend_ttl(self, key: str, ttl: Optional[int] = None) -> bool:
        """
        Extend session expiration time.

        Args:
            key: Session identifier
            ttl: New TTL in seconds (default 1800 = 30 minutes)

        Returns:
            True if TTL extended, False if session doesn't exist
        """
        async with self._lock:
            if key not in self._sessions:
                return False

            # Check if already expired
            if key in self._ttls and datetime.now(timezone.utc) > self._ttls[key]:
                await self._cleanup_session(key)
                return False

            ttl = ttl or 1800
            self._ttls[key] = datetime.now(timezone.utc) + timedelta(seconds=ttl)
            return True

    async def find_by_user_and_client(self, user_id: str, client_id: str) -> Optional[str]:
        """
        Find session ID by user_id and client_id combination.

        Args:
            user_id: User identifier
            client_id: Client/device identifier

        Returns:
            Session ID if found, None if not found or expired
        """
        async with self._lock:
            index_key = f"{user_id}:{client_id}"
            session_id = self._client_index.get(index_key)

            if not session_id:
                return None

            # Verify session still exists and isn't expired
            if session_id in self._ttls:
                if datetime.now(timezone.utc) > self._ttls[session_id]:
                    await self._cleanup_session(session_id)
                    return None

            return session_id if session_id in self._sessions else None

    async def index_session_by_client(self, user_id: str, client_id: str, session_id: str, ttl: int) -> None:
        """
        Create or update client index entry.

        Args:
            user_id: User identifier
            client_id: Client/device identifier
            session_id: Session ID to index
            ttl: Time to live in seconds
        """
        async with self._lock:
            index_key = f"{user_id}:{client_id}"
            self._client_index[index_key] = session_id

    async def remove_client_index(self, user_id: str, client_id: str) -> None:
        """
        Remove client index entry.

        Args:
            user_id: User identifier
            client_id: Client/device identifier
        """
        async with self._lock:
            index_key = f"{user_id}:{client_id}"
            if index_key in self._client_index:
                del self._client_index[index_key]

    async def _cleanup_session(self, key: str) -> None:
        """
        Internal method to clean up expired session and its indexes.

        Args:
            key: Session identifier to clean up
        """
        # Remove session data
        if key in self._sessions:
            del self._sessions[key]

        # Remove TTL
        if key in self._ttls:
            del self._ttls[key]

        # Remove client indexes pointing to this session
        indexes_to_remove = [
            index_key for index_key, session_id in self._client_index.items()
            if session_id == key
        ]
        for index_key in indexes_to_remove:
            del self._client_index[index_key]
