"""session_management.py

Purpose: Stateful session handling for troubleshooting using Redis

Requirements:
--------------------------------------------------------------------------------
• Implement SessionManager class with Redis-based session lifecycle methods
• Define SessionContext Pydantic model
• Use Redis for distributed, scalable session storage

Key Components:
--------------------------------------------------------------------------------
  class SessionManager: create_session(), get_session()
  class SessionContext(BaseModel): ...
  Redis-based session persistence with automatic expiration

Technology Stack:
--------------------------------------------------------------------------------
Pydantic, AsyncIO, Redis

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Cost-Efficiency: Use semantic caching
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import redis.asyncio as redis

from .models import AgentState, SessionContext


class SessionManager:
    """Manages user sessions for troubleshooting investigations using Redis"""

    def __init__(
        self, redis_url: str = "redis://localhost:6379", session_timeout_hours: int = 24
    ):
        """
        Initialize SessionManager with Redis connection

        Args:
            redis_url: Redis connection URL
            session_timeout_hours: Session timeout in hours
        """
        self.logger = logging.getLogger(__name__)
        self.redis_client = redis.from_url(redis_url)
        self.session_timeout = timedelta(hours=session_timeout_hours)
        self.session_timeout_seconds = int(self.session_timeout.total_seconds())

        self.logger.info(f"SessionManager initialized with Redis at {redis_url}")

    async def create_session(self, user_id: Optional[str] = None) -> SessionContext:
        """
        Create a new session for a user

        Args:
            user_id: Optional user identifier

        Returns:
            New session context
        """
        session_id = str(uuid.uuid4())

        session = SessionContext(
            session_id=session_id,
            user_id=user_id,
            created_at=datetime.utcnow(),
            last_activity=datetime.utcnow(),
            agent_state=None,
        )

        # Store in Redis with expiration
        session_key = f"session:{session_id}"
        await self.redis_client.set(
            session_key, session.model_dump_json(), ex=self.session_timeout_seconds
        )

        self.logger.info(f"Created new Redis session: {session_id}")
        return session

    async def get_session(self, session_id: str) -> Optional[SessionContext]:
        """
        Retrieve a session by ID

        Args:
            session_id: Session identifier

        Returns:
            Session context or None if not found
        """
        session_key = f"session:{session_id}"
        session_data = await self.redis_client.get(session_key)

        if session_data:
            try:
                # Deserialize from JSON back to a Pydantic model
                session = SessionContext.model_validate_json(session_data)

                # Update last activity and refresh expiration
                session.last_activity = datetime.utcnow()
                await self.redis_client.set(
                    session_key,
                    session.model_dump_json(),
                    ex=self.session_timeout_seconds,
                )

                self.logger.debug(f"Retrieved session: {session_id}")
                return session

            except Exception as e:
                self.logger.error(f"Failed to deserialize session {session_id}: {e}")
                # Clean up corrupted session
                await self.redis_client.delete(session_key)
                return None

        return None

    async def update_session(self, session_id: str, updates: Dict) -> bool:
        """
        Update session with new data

        Args:
            session_id: Session identifier
            updates: Dictionary of updates to apply

        Returns:
            True if update was successful
        """
        session = await self.get_session(session_id)

        if not session:
            self.logger.warning(
                f"Attempted to update non-existent session: {session_id}"
            )
            return False

        try:
            # Update session fields
            for key, value in updates.items():
                if hasattr(session, key):
                    setattr(session, key, value)

            # Update last activity
            session.last_activity = datetime.utcnow()

            # Save back to Redis
            session_key = f"session:{session_id}"
            await self.redis_client.set(
                session_key, session.model_dump_json(), ex=self.session_timeout_seconds
            )

            self.logger.debug(f"Updated session: {session_id}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to update session {session_id}: {e}")
            return False

    async def add_data_upload(self, session_id: str, data_id: str) -> bool:
        """
        Add a data upload to a session

        Args:
            session_id: Session identifier
            data_id: Data upload identifier

        Returns:
            True if addition was successful
        """
        session = await self.get_session(session_id)

        if not session:
            self.logger.warning(
                f"Attempted to add data to non-existent session: {session_id}"
            )
            return False

        if data_id not in session.data_uploads:
            session.data_uploads.append(data_id)
            session.last_activity = datetime.utcnow()

            # Save back to Redis
            session_key = f"session:{session_id}"
            await self.redis_client.set(
                session_key, session.model_dump_json(), ex=self.session_timeout_seconds
            )

            self.logger.debug(f"Added data upload {data_id} to session {session_id}")

        return True

    async def add_investigation_history(
        self, session_id: str, investigation_data: Dict
    ) -> bool:
        """
        Add investigation history to a session

        Args:
            session_id: Session identifier
            investigation_data: Investigation data to add

        Returns:
            True if addition was successful
        """
        session = await self.get_session(session_id)

        if not session:
            self.logger.warning(
                f"Attempted to add history to non-existent session: {session_id}"
            )
            return False

        investigation_data["timestamp"] = datetime.utcnow().isoformat()
        session.investigation_history.append(investigation_data)
        session.last_activity = datetime.utcnow()

        # Save back to Redis
        session_key = f"session:{session_id}"
        await self.redis_client.set(
            session_key, session.model_dump_json(), ex=self.session_timeout_seconds
        )

        self.logger.debug(f"Added investigation history to session {session_id}")
        return True

    async def update_agent_state(
        self, session_id: str, agent_state: AgentState
    ) -> bool:
        """
        Update the agent state for a session

        Args:
            session_id: Session identifier
            agent_state: New agent state

        Returns:
            True if update was successful
        """
        session = await self.get_session(session_id)

        if not session:
            self.logger.warning(
                f"Attempted to update agent state for non-existent session: {session_id}"
            )
            return False

        session.agent_state = agent_state
        session.last_activity = datetime.utcnow()

        # Save back to Redis
        session_key = f"session:{session_id}"
        await self.redis_client.set(
            session_key, session.model_dump_json(), ex=self.session_timeout_seconds
        )

        self.logger.debug(f"Updated agent state for session {session_id}")
        return True

    async def delete_session(self, session_id: str) -> bool:
        """
        Delete a session from Redis

        Args:
            session_id: Session identifier

        Returns:
            True if session was deleted successfully
        """
        try:
            session_key = f"session:{session_id}"
            result = await self.redis_client.delete(session_key)

            if result > 0:
                self.logger.info(f"Deleted session: {session_id}")
                return True
            else:
                self.logger.warning(f"Session not found for deletion: {session_id}")
                return False

        except Exception as e:
            self.logger.error(f"Failed to delete session {session_id}: {e}")
            return False

    async def list_sessions(
        self, user_id: Optional[str] = None, pattern: str = "session:*"
    ) -> List[SessionContext]:
        """
        List all sessions matching the given pattern, optionally filtered by user_id

        Args:
            user_id: Optional user ID to filter sessions by
            pattern: Redis key pattern to match

        Returns:
            List of session contexts
        """
        try:
            # Get all session keys
            session_keys = await self.redis_client.keys(pattern)
            sessions = []

            for key in session_keys:
                session_data = await self.redis_client.get(key)
                if session_data:
                    try:
                        session = SessionContext.model_validate_json(session_data)

                        # Filter by user_id if specified
                        if user_id is None or session.user_id == user_id:
                            sessions.append(session)
                    except Exception as e:
                        self.logger.warning(
                            f"Failed to parse session from key {key}: {e}"
                        )
                        continue

            self.logger.info(
                f"Listed {len(sessions)} sessions"
                + (f" for user {user_id}" if user_id else "")
            )
            return sessions

        except Exception as e:
            self.logger.error(f"Failed to list sessions: {e}")
            return []

    async def get_session_stats(self) -> Dict:
        """
        Get statistics about all sessions

        Returns:
            Dictionary with session statistics
        """
        sessions = await self.list_sessions()
        total_sessions = len(sessions)

        # Calculate active sessions (last activity within 1 hour)
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        active_sessions = len([s for s in sessions if s.last_activity > one_hour_ago])

        # Calculate average session duration
        durations = []
        for session in sessions:
            duration = datetime.utcnow() - session.created_at
            durations.append(duration.total_seconds() / 3600)  # Convert to hours

        avg_duration = sum(durations) / len(durations) if durations else 0

        # Count sessions by user
        user_sessions: Dict[str, int] = {}
        for session in sessions:
            user_id = session.user_id or "anonymous"
            user_sessions[user_id] = user_sessions.get(user_id, 0) + 1

        return {
            "total_sessions": total_sessions,
            "active_sessions": active_sessions,
            "average_duration_hours": avg_duration,
            "sessions_by_user": user_sessions,
            "oldest_session": (
                min([s.created_at for s in sessions]) if sessions else None
            ),
            "newest_session": (
                max([s.created_at for s in sessions]) if sessions else None
            ),
        }

    async def is_session_active(self, session_id: str) -> bool:
        """
        Check if a session is still active

        Args:
            session_id: Session identifier

        Returns:
            True if session is active
        """
        session_key = f"session:{session_id}"
        exists = await self.redis_client.exists(session_key)
        return bool(exists)

    async def extend_session(self, session_id: str) -> bool:
        """
        Extend a session's timeout by updating last activity

        Args:
            session_id: Session identifier

        Returns:
            True if extension was successful
        """
        session = await self.get_session(session_id)

        if not session:
            return False

        session.last_activity = datetime.utcnow()

        # Save back to Redis with extended expiration
        session_key = f"session:{session_id}"
        await self.redis_client.set(
            session_key, session.model_dump_json(), ex=self.session_timeout_seconds
        )

        self.logger.debug(f"Extended session: {session_id}")
        return True

    async def update_last_activity(self, session_id: str) -> bool:
        """
        Update the last activity timestamp for a session

        Args:
            session_id: Session identifier

        Returns:
            True if session was updated successfully
        """
        try:
            session = await self.get_session(session_id)
            if not session:
                return False

            # Update last activity
            session.last_activity = datetime.utcnow()

            # Store updated session in Redis
            session_key = f"session:{session_id}"
            await self.redis_client.set(
                session_key, session.model_dump_json(), ex=self.session_timeout_seconds
            )

            self.logger.info(f"Updated last activity for session: {session_id}")
            return True

        except Exception as e:
            self.logger.error(
                f"Failed to update last activity for session {session_id}: {e}"
            )
            return False

    async def close(self):
        """
        Close the Redis connection
        """
        await self.redis_client.close()
        self.logger.info("Redis connection closed")
