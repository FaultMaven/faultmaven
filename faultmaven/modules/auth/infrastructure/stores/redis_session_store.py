"""
Redis implementation of ISessionStore interface.

This module provides a Redis-based session store that implements
the ISessionStore interface. Works with both real Redis (cloud)
and FakeRedis (local deployment) — the client is injected via DI.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from faultmaven.models.common import SessionContext
from faultmaven.models.interfaces import ISessionStore
from faultmaven.utils.datetime import parse_utc_timestamp
from faultmaven.utils.serialization import to_json_compatible

logger = logging.getLogger(__name__)


class RedisSessionStore(ISessionStore):
    """Redis implementation of the ISessionStore interface.

    Works identically with real Redis and FakeRedis.
    """

    def __init__(self, redis_client):
        """Initialize Redis session store.

        Args:
            redis_client: Async Redis-compatible client (real or FakeRedis).
        """
        self.redis_client = redis_client
        self.default_ttl = 1800  # 30 minutes default
        self.prefix = "session:"

    async def get(self, key: str) -> Optional[Dict]:
        """Get session data by key."""
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
            logger.error(f"Redis get operation failed for key {key}: {e}")
            raise ConnectionError(f"Redis operation failed: {e}")

    async def set(
        self, key: str, value: Union[Dict, Any], ttl: Optional[int] = None
    ) -> None:
        """Set session data with optional TTL."""
        try:
            full_key = f"{self.prefix}{key}"

            if isinstance(value, dict):
                if "last_activity" not in value:
                    value["last_activity"] = to_json_compatible(
                        datetime.now(timezone.utc)
                    )
                value = to_json_compatible(value)
                serialized = json.dumps(value)
            else:
                serialized = json.dumps(value)

            ttl = ttl if ttl is not None else self.default_ttl

            await self.redis_client.set(full_key, serialized, ex=ttl)
        except Exception as e:
            logger.error(f"Redis set operation failed for key {key}: {e}")
            raise ConnectionError(f"Redis operation failed: {e}")

    async def delete(self, key: str) -> bool:
        """Delete session by key."""
        full_key = f"{self.prefix}{key}"
        result = await self.redis_client.delete(full_key)
        return result > 0

    async def exists(self, key: str) -> bool:
        """Check if session exists."""
        full_key = f"{self.prefix}{key}"
        return await self.redis_client.exists(full_key) > 0

    async def extend_ttl(self, key: str, ttl: Optional[int] = None) -> bool:
        """Extend session TTL."""
        full_key = f"{self.prefix}{key}"
        ttl = ttl if ttl is not None else self.default_ttl
        return await self.redis_client.expire(full_key, ttl)

    async def find_by_user_and_client(
        self, user_id: str, client_id: str
    ) -> Optional[str]:
        """Find session ID by user_id and client_id combination."""
        try:
            client_index_key = f"client_index:{user_id}:{client_id}"
            session_id = await self.redis_client.get(client_index_key)

            if session_id:
                if isinstance(session_id, bytes):
                    session_id = session_id.decode("utf-8")
                return session_id
            return None
        except Exception as e:
            logger.error(
                f"Redis find_by_user_and_client failed for user {user_id}, "
                f"client {client_id}: {e}"
            )
            raise ConnectionError(f"Redis operation failed: {e}")

    async def index_session_by_client(
        self, user_id: str, client_id: str, session_id: str, ttl: int
    ) -> None:
        """Create or update client index entry."""
        try:
            client_index_key = f"client_index:{user_id}:{client_id}"
            pipe = self.redis_client.pipeline()
            pipe.set(client_index_key, session_id, ex=ttl)
            await pipe.execute()
        except Exception as e:
            logger.error(
                f"Redis index_session_by_client failed for user {user_id}, "
                f"client {client_id}, session {session_id}: {e}"
            )
            raise ConnectionError(f"Redis operation failed: {e}")

    async def remove_client_index(self, user_id: str, client_id: str) -> None:
        """Remove client index entry."""
        try:
            client_index_key = f"client_index:{user_id}:{client_id}"
            await self.redis_client.delete(client_index_key)
        except Exception as e:
            logger.warning(
                f"Redis remove_client_index failed for user {user_id}, "
                f"client {client_id}: {e}"
            )

    async def increment_counter(self, key: str, ttl: Optional[int] = None) -> int:
        """Increment an atomic counter."""
        try:
            full_key = f"{self.prefix}{key}"
            value = await self.redis_client.incr(full_key)

            if ttl and value == 1:
                await self.redis_client.expire(full_key, ttl)

            return value
        except Exception as e:
            logger.error(f"Redis increment operation failed for key {key}: {e}")
            raise ConnectionError(f"Redis operation failed: {e}")

    # High-level session management methods

    async def create_session(self, user_id: Optional[str] = None) -> SessionContext:
        """Create a new session."""
        session_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc)

        session_data = {
            "session_id": session_id,
            "user_id": user_id,
            "created_at": to_json_compatible(created_at),
            "last_activity": to_json_compatible(created_at),
            "data_uploads": [],
            "case_history": [],
            "metadata": {},
        }

        await self.set(session_id, session_data)

        return SessionContext(
            session_id=session_id,
            user_id=user_id,
            created_at=created_at,
            last_activity=created_at,
            data_uploads=[],
            case_history=[],
            metadata={},
        )

    async def get_session(
        self, session_id: str, validate: bool = True
    ) -> Optional[SessionContext]:
        """Get session by ID."""
        session_data = await self.get(session_id)
        if not session_data:
            return None

        created_at = parse_utc_timestamp(session_data["created_at"])
        last_activity = parse_utc_timestamp(session_data["last_activity"])
        updated_at = parse_utc_timestamp(
            session_data.get("updated_at", session_data["last_activity"])
        )
        expires_at = (
            parse_utc_timestamp(session_data["expires_at"])
            if session_data.get("expires_at")
            else None
        )

        return SessionContext(
            session_id=session_data["session_id"],
            user_id=session_data.get("user_id"),
            client_id=session_data.get("client_id"),
            session_resumed=session_data.get("session_resumed", False),
            created_at=created_at,
            last_activity=last_activity,
            updated_at=updated_at,
            expires_at=expires_at,
            metadata=session_data.get("metadata", {}),
        )

    async def save(self, session: SessionContext) -> None:
        """Save session context to Redis."""
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
        await self.set(session.session_id, session_data)

    async def update_session(self, session_id: str, updates: Dict[str, Any]) -> bool:
        """Update session with new data."""
        session_data = await self.get(session_id)
        if not session_data:
            return False

        session_data.update(updates)
        session_data["last_activity"] = to_json_compatible(datetime.now(timezone.utc))

        await self.set(session_id, session_data)
        return True

    async def delete_session(self, session_id: str) -> bool:
        """Delete session."""
        return await self.delete(session_id)

    async def extend_session(self, session_id: str) -> bool:
        """Extend session TTL."""
        return await self.extend_ttl(session_id)

    async def update_last_activity(self, session_id: str) -> bool:
        """Update last activity timestamp."""
        return await self.update_session(session_id, {})

    async def list_sessions(
        self, user_id: Optional[str] = None
    ) -> List[SessionContext]:
        """List sessions, optionally filtered by user_id."""
        # Note: Listing all sessions requires SCAN, which is expensive.
        # This is a simplified implementation.
        logger.warning(
            "list_sessions is not fully implemented - would need session indexing"
        )
        return []

    async def get_all_sessions(self) -> List[SessionContext]:
        """Get all sessions."""
        return await self.list_sessions()

    async def get_session_stats(self) -> Dict[str, Any]:
        """Get session statistics."""
        return {
            "total_sessions": 0,
            "active_sessions": 0,
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
        }
