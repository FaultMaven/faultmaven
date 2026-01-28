"""
In-memory implementation of ISessionStore interface.

This module provides a RAM-based session store for development and testing.
Data is stored in Python dictionaries and lost on application restart.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from faultmaven.models.common import SessionContext
from faultmaven.models.interfaces import ISessionStore
from faultmaven.utils.datetime import parse_utc_timestamp
from faultmaven.utils.serialization import to_json_compatible


class InMemorySessionStore(ISessionStore):
    """In-memory implementation of ISessionStore using Python dictionaries"""

    def __init__(self):
        """Initialize in-memory session store with Python dicts"""
        self._sessions: Dict[str, Dict] = {}  # session_id -> session_data
        self._client_index: Dict[str, str] = {}  # "user_id:client_id" -> session_id
        self._counters: Dict[str, int] = {}  # key -> count
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
            if "last_activity" not in value:
                value["last_activity"] = datetime.now(timezone.utc).isoformat()

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

    async def find_by_user_and_client(
        self, user_id: str, client_id: str
    ) -> Optional[str]:
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

    async def index_session_by_client(
        self, user_id: str, client_id: str, session_id: str, ttl: int
    ) -> None:
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

    async def increment_counter(self, key: str, ttl: Optional[int] = None) -> int:
        """
        Increment an atomic counter.

        Args:
            key: Counter identifier
            ttl: Time to live in seconds (optional)

        Returns:
            The new value of the counter after incrementing
        """
        async with self._lock:
            # Check expiration
            if key in self._ttls and datetime.now(timezone.utc) > self._ttls[key]:
                if key in self._counters:
                    del self._counters[key]
                if key in self._ttls:
                    del self._ttls[key]

            current_value = self._counters.get(key, 0)
            new_value = current_value + 1
            self._counters[key] = new_value

            if ttl and new_value == 1:
                self._ttls[key] = datetime.now(timezone.utc) + timedelta(seconds=ttl)

            return new_value

    async def get_session(self, session_id: str) -> Optional[SessionContext]:
        """
        Get session by ID and return as SessionContext object.

        Args:
            session_id: Session identifier

        Returns:
            SessionContext if found and not expired, None otherwise
        """
        session_data = await self.get(session_id)
        if not session_data:
            return None

        # Convert dict to SessionContext
        try:
            created_at = parse_utc_timestamp(session_data.get("created_at"))
            last_activity = parse_utc_timestamp(
                session_data.get("last_activity", session_data.get("created_at"))
            )
            updated_at = parse_utc_timestamp(
                session_data.get(
                    "updated_at",
                    session_data.get("last_activity", session_data.get("created_at")),
                )
            )
            expires_at = self._ttls.get(session_id)

            return SessionContext(
                session_id=session_id,
                user_id=session_data.get("user_id"),
                client_id=session_data.get("client_id"),
                session_resumed=session_data.get("session_resumed", False),
                created_at=created_at,
                last_activity=last_activity,
                updated_at=updated_at,
                expires_at=expires_at,
                metadata=session_data.get("metadata", {}),
            )
        except Exception as e:
            import logging

            logger = logging.getLogger(__name__)
            logger.warning(
                f"Failed to convert session {session_id} to SessionContext: {e}"
            )
            return None

    async def save(self, session: SessionContext) -> None:
        """
        Save session context to in-memory store.

        Args:
            session: SessionContext object to save
        """
        session_data = {
            "session_id": session.session_id,
            "user_id": session.user_id,
            "client_id": session.client_id,
            "created_at": to_json_compatible(session.created_at),
            "last_activity": to_json_compatible(session.last_activity),
            "updated_at": to_json_compatible(session.updated_at),
            "expires_at": (
                to_json_compatible(session.expires_at) if session.expires_at else None
            ),
            "metadata": session.metadata or {},
            "session_resumed": session.session_resumed,
        }

        # Calculate TTL from expires_at if available
        ttl = None
        if session.expires_at:
            now = datetime.now(timezone.utc)
            if session.expires_at > now:
                ttl = int((session.expires_at - now).total_seconds())
            else:
                # Already expired, don't save
                return

        await self.set(session.session_id, session_data, ttl=ttl)

    async def list_sessions(
        self, user_id: Optional[str] = None
    ) -> List[SessionContext]:
        """
        List sessions, optionally filtered by user_id.

        Args:
            user_id: Optional user ID to filter sessions

        Returns:
            List of SessionContext objects
        """
        async with self._lock:
            sessions = []
            now = datetime.now(timezone.utc)

            for session_id, session_data in self._sessions.items():
                # Skip expired sessions
                if session_id in self._ttls and now > self._ttls[session_id]:
                    continue

                # Filter by user_id if provided
                session_user_id = session_data.get("user_id")
                if user_id and session_user_id != user_id:
                    continue

                # user_id is required for SessionContext
                if not session_user_id:
                    continue

                # Convert dict to SessionContext
                try:
                    created_at = parse_utc_timestamp(session_data.get("created_at"))
                    last_activity = parse_utc_timestamp(
                        session_data.get(
                            "last_activity", session_data.get("created_at")
                        )
                    )
                    updated_at = parse_utc_timestamp(
                        session_data.get(
                            "updated_at",
                            session_data.get(
                                "last_activity", session_data.get("created_at")
                            ),
                        )
                    )
                    expires_at = self._ttls.get(session_id)

                    session_context = SessionContext(
                        session_id=session_id,
                        user_id=session_user_id,
                        client_id=session_data.get("client_id"),
                        session_resumed=session_data.get("session_resumed", False),
                        created_at=created_at,
                        last_activity=last_activity,
                        updated_at=updated_at,
                        expires_at=expires_at,
                        metadata=session_data.get("metadata", {}),
                    )
                    sessions.append(session_context)
                except Exception as e:
                    # Skip sessions with invalid data
                    import logging

                    logger = logging.getLogger(__name__)
                    logger.warning(
                        f"Failed to convert session {session_id} to SessionContext: {e}"
                    )
                    continue

            return sessions

    async def _cleanup_session(self, key: str) -> None:
        """
        Internal method to clean up expired session and its indexes.

        Args:
            key: Session identifier to clean up
        """
        # Remove session data
        if key in self._sessions:
            del self._sessions[key]

        # Remove counter data
        if key in self._counters:
            del self._counters[key]

        # Remove TTL
        if key in self._ttls:
            del self._ttls[key]

        # Remove client indexes pointing to this session
        indexes_to_remove = [
            index_key
            for index_key, session_id in self._client_index.items()
            if session_id == key
        ]
        for index_key in indexes_to_remove:
            del self._client_index[index_key]
