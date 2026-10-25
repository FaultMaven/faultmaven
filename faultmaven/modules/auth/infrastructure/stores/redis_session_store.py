"""
Redis implementation of ISessionStore interface.

This module provides a Redis-based session store that implements
the ISessionStore interface. Works with both real Redis (cloud)
and FakeRedis (local deployment) — the client is injected via DI.

Each user's sessions are indexed in a set, ``sessions:user:<user_id>``, so a
user's sessions are a set read rather than a keyspace scan (#2297). The store
lists one user's sessions and never every user's: nothing in production reads
such a listing, and enumerating sessions across users is what #1447 closed.
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

#: Compare-and-delete: delete the key only while it still holds the value.
_DELETE_IF_HOLDS = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

#: Write a session and index it under its user, in one step. The index's TTL is
#: raised to the session's and never lowered, so the index lapses with its
#: longest-lived member: Redis tells nobody when a key's TTL lapses, and an index
#: without a TTL would outlive every session it names.
_SAVE_AND_INDEX = """
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('SADD', KEYS[2], ARGV[3])
if redis.call('TTL', KEYS[2]) < tonumber(ARGV[2]) then
    redis.call('EXPIRE', KEYS[2], ARGV[2])
end
return 1
"""

#: Extend a session's TTL and, when the session exists, index it and raise the
#: index's TTL to match, as ``_SAVE_AND_INDEX`` does.
_EXTEND_AND_INDEX = """
if redis.call('EXPIRE', KEYS[1], ARGV[1]) == 0 then
    return 0
end
redis.call('SADD', KEYS[2], ARGV[2])
if redis.call('TTL', KEYS[2]) < tonumber(ARGV[1]) then
    redis.call('EXPIRE', KEYS[2], ARGV[1])
end
return 1
"""

#: Read the members' sessions, and drop from the index each member whose session
#: key is gone, in one step: a session re-written between a separate read and
#: SREM would lose its index entry. KEYS[1] is the index, KEYS[2..n] the members'
#: session keys, and ARGV the members in the same order.
_READ_AND_PRUNE = """
local live = {}
for i, member in ipairs(ARGV) do
    local value = redis.call('GET', KEYS[i + 1])
    if value then
        live[#live + 1] = value
    else
        redis.call('SREM', KEYS[1], member)
    end
end
return live
"""


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
        #: Outside ``prefix``: ``get``/``set`` take caller-supplied session ids
        #: under it, and ``delete_keys_holding`` scans it.
        self.user_index_prefix = "sessions:user:"

    def _user_index_key(self, user_id: str) -> str:
        """The set holding the ids of ``user_id``'s sessions."""
        return f"{self.user_index_prefix}{user_id}"

    @staticmethod
    def _serialize(value: Union[Dict, Any]) -> str:
        """``value`` as ``set`` stores it; a dict gains ``last_activity`` if absent."""
        if isinstance(value, dict):
            if "last_activity" not in value:
                value["last_activity"] = to_json_compatible(datetime.now(timezone.utc))
            value = to_json_compatible(value)
        return json.dumps(value)

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
            serialized = self._serialize(value)
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

    async def delete_keys_holding(self, match: str, value: Any) -> int:
        """SCAN the prefixed ``match`` and compare-and-delete each key against
        ``value`` serialised as ``set`` stores it (``json.dumps``)."""
        stored = json.dumps(value)
        deleted = 0
        async for key in self.redis_client.scan_iter(
            match=f"{self.prefix}{match}", count=1000
        ):
            deleted += int(
                await self.redis_client.eval(_DELETE_IF_HOLDS, 1, key, stored)
            )
        return deleted

    # High-level session management methods. Every session write goes through
    # ``_write_session`` and every session delete through ``delete_session``,
    # so the user index follows them. ``set``, ``extend_ttl`` and ``delete``
    # above are plain key operations and do not touch the index.

    async def _write_session(
        self, session_id: str, session_data: Dict[str, Any]
    ) -> None:
        """Store a session for ``default_ttl``, indexed under its user when it has one."""
        user_id = session_data.get("user_id")
        if not user_id:
            await self.set(session_id, session_data)
            return

        try:
            await self.redis_client.eval(
                _SAVE_AND_INDEX,
                2,
                f"{self.prefix}{session_id}",
                self._user_index_key(user_id),
                self._serialize(session_data),
                self.default_ttl,
                session_id,
            )
        except Exception as e:
            logger.error(f"Redis session write failed for session {session_id}: {e}")
            raise ConnectionError(f"Redis operation failed: {e}")

    @staticmethod
    def _to_session_context(session_data: Dict[str, Any]) -> SessionContext:
        """A stored session record as a ``SessionContext``."""
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

        await self._write_session(session_id, session_data)

        return SessionContext(
            session_id=session_id,
            user_id=user_id,
            created_at=created_at,
            last_activity=created_at,
            metadata={},
        )

    async def get_session(
        self, session_id: str, validate: bool = True
    ) -> Optional[SessionContext]:
        """Get session by ID."""
        session_data = await self.get(session_id)
        if not session_data:
            return None
        return self._to_session_context(session_data)

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
        await self._write_session(session.session_id, session_data)

    async def update_session(self, session_id: str, updates: Dict[str, Any]) -> bool:
        """Update session with new data."""
        session_data = await self.get(session_id)
        if not session_data:
            return False

        session_data.update(updates)
        session_data["last_activity"] = to_json_compatible(datetime.now(timezone.utc))

        await self._write_session(session_id, session_data)
        return True

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session and its entry in its user's index."""
        session_data = await self.get(session_id)
        user_id = (
            session_data.get("user_id") if isinstance(session_data, dict) else None
        )
        if not user_id:
            return await self.delete(session_id)

        pipe = self.redis_client.pipeline(transaction=True)
        pipe.delete(f"{self.prefix}{session_id}")
        pipe.srem(self._user_index_key(user_id), session_id)
        deleted, _ = await pipe.execute()
        return deleted > 0

    async def extend_session(self, session_id: str) -> bool:
        """Extend session TTL, and its user index's with it."""
        session_data = await self.get(session_id)
        user_id = (
            session_data.get("user_id") if isinstance(session_data, dict) else None
        )
        if not user_id:
            return await self.extend_ttl(session_id)

        extended = await self.redis_client.eval(
            _EXTEND_AND_INDEX,
            2,
            f"{self.prefix}{session_id}",
            self._user_index_key(user_id),
            self.default_ttl,
            session_id,
        )
        return bool(extended)

    async def update_last_activity(self, session_id: str) -> bool:
        """Update last activity timestamp."""
        return await self.update_session(session_id, {})

    async def list_sessions(self, user_id: str) -> List[SessionContext]:
        """``user_id``'s live sessions, oldest first.

        Reads the user's index and prunes from it every session whose key has
        expired or been deleted. A session written before the index existed is
        not listed until it is written again.
        """
        index_key = self._user_index_key(user_id)
        try:
            members = sorted(
                m.decode("utf-8") if isinstance(m, bytes) else m
                for m in await self.redis_client.smembers(index_key)
            )
            if not members:
                return []
            values = await self.redis_client.eval(
                _READ_AND_PRUNE,
                1 + len(members),
                index_key,
                *(f"{self.prefix}{member}" for member in members),
                *members,
            )
        except Exception as e:
            logger.error(f"Redis list_sessions failed for user {user_id}: {e}")
            raise ConnectionError(f"Redis operation failed: {e}")

        sessions = []
        for value in values:
            try:
                session_data = json.loads(value)
                session = self._to_session_context(session_data)
            except (ValueError, KeyError, TypeError) as e:
                logger.warning(f"Skipping unreadable session of user {user_id}: {e}")
                continue
            # The index names this user's sessions; the record is the authority.
            if session.user_id == user_id:
                sessions.append(session)

        sessions.sort(key=lambda s: s.created_at)
        return sessions
