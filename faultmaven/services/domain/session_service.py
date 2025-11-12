"""Session Service Module - Spec-Compliant (v2.0)

Purpose: Authentication session management ONLY (per case-and-session-concepts.md)

Core Responsibilities (Spec Lines 388-423):
- Session lifecycle management (create, validate, expire, delete)
- Multi-device support via client_id (spec lines 263-269)
- Session resumption for same client_id (spec lines 83-86)
- Session authentication and authorization
- Session analytics and monitoring
- Session cleanup and maintenance

NOT Responsible For (Spec Lines 102-107):
- Case management (cases are independent resources)
- Case history (cases manage their own history)
- Current case tracking (frontend UI state)
- Data uploads (belongs to cases)

Version: 2.0 (Spec-Compliant Refactor - 2025-10-23)
Reference: docs/architecture/case-and-session-concepts.md
"""

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from faultmaven.services.base import BaseService
from faultmaven.models import SessionContext
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import ValidationException, ServiceException
from faultmaven.utils.serialization import to_json_compatible


class SessionService(BaseService):
    """Service for authentication session management (spec-compliant)

    Implements multi-device session support per spec lines 88-92, 213-246.
    Sessions provide AUTHENTICATION ONLY - no case management.
    """

    def __init__(
        self,
        session_store: Optional[Any] = None,
        settings: Optional[Any] = None,
        max_sessions_per_user: int = 10,
        inactive_threshold_hours: int = 24,
        session_ttl_hours: int = 24,
    ):
        """Initialize Session Service

        Args:
            session_store: Session persistence store (ISessionStore interface)
            settings: Configuration settings
            max_sessions_per_user: Maximum concurrent sessions per user (multi-device)
            inactive_threshold_hours: Hours before marking session inactive
            session_ttl_hours: Session expiration TTL
        """
        super().__init__("session_service")
        self.session_store = session_store
        self._settings = settings

        # Use settings if available, otherwise use defaults
        if settings and hasattr(settings, 'session'):
            self.max_sessions_per_user = getattr(settings.session, 'max_sessions_per_user', max_sessions_per_user)
            timeout_hours = getattr(settings.session, 'timeout_minutes', inactive_threshold_hours * 60) / 60
            self.inactive_threshold = timedelta(hours=timeout_hours)
            self.session_ttl = timedelta(hours=getattr(settings.session, 'ttl_hours', session_ttl_hours))
        else:
            self.max_sessions_per_user = max_sessions_per_user
            self.inactive_threshold = timedelta(hours=inactive_threshold_hours)
            self.session_ttl = timedelta(hours=session_ttl_hours)

        # Compatibility alias
        self.session_manager = self.session_store

    # =========================================================================
    # Core Session Lifecycle Management
    # =========================================================================

    @trace("session_service_create_session")
    async def create_session(
        self,
        user_id: str,
        client_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[SessionContext, bool]:
        """Create new session or resume existing session for (user, client) pair

        Implements session resumption per spec lines 83-86, 394-416.

        Args:
            user_id: User identifier (REQUIRED per spec)
            client_id: Client/device identifier for multi-device support (spec line 264)
            metadata: Additional session metadata (authentication context only)

        Returns:
            Tuple of (SessionContext, resumed: bool)

        Raises:
            ValidationException: If user_id is invalid
            ServiceException: If session creation fails
        """
        if not user_id or not user_id.strip():
            raise ValidationException("user_id is required for session creation")

        # Check for existing session for this (user_id, client_id) pair
        # Implements session resumption (spec lines 397-402)
        if client_id:
            existing_session = await self._find_session_by_user_and_client(user_id, client_id)
            if existing_session:
                # Session resumption - extend expiry and return
                await self.extend_session(existing_session.session_id)
                existing_session.session_resumed = True
                self.logger.info(
                    f"Session resumed for user {user_id}, client {client_id}: {existing_session.session_id}"
                )
                return existing_session, True

        # Enforce max sessions per user (multi-device limit)
        await self._enforce_session_limit(user_id)

        # Create new session
        now = datetime.now(timezone.utc)
        session = SessionContext(
            session_id=str(uuid4()),
            user_id=user_id,
            client_id=client_id,
            session_resumed=False,
            created_at=now,
            last_activity=now,
            updated_at=now,
            expires_at=now + self.session_ttl,
            metadata=metadata or {}
        )

        # Persist session
        if self.session_store:
            await self.session_store.save(session)

            # Index by (user_id, client_id) for resumption if client_id provided
            if client_id:
                await self.session_store.index_by_user_and_client(user_id, client_id, session.session_id)

        self.logger.info(f"Created new session for user {user_id}: {session.session_id}")
        return session, False

    @trace("session_service_get_session")
    async def get_session(self, session_id: str) -> Optional[SessionContext]:
        """Get session by ID

        Args:
            session_id: Session identifier

        Returns:
            SessionContext if found and valid, None otherwise
        """
        if not session_id or not session_id.strip():
            return None

        if not self.session_store:
            raise ServiceException("Session store not configured")

        session = await self.session_store.get(session_id)
        if not session:
            return None

        # Check if session is expired
        if session.expires_at and datetime.now(timezone.utc) > session.expires_at:
            self.logger.info(f"Session {session_id} has expired, deleting")
            await self.delete_session(session_id)
            return None

        return session

    @trace("session_service_validate_session")
    async def validate_session(self, session_id: str) -> bool:
        """Validate session is active and not expired

        Args:
            session_id: Session identifier

        Returns:
            True if session is valid and active
        """
        session = await self.get_session(session_id)
        return session is not None

    @trace("session_service_get_user_from_session")
    async def get_user_from_session(self, session_id: str) -> Optional[str]:
        """Get user_id from session for authorization (spec line 421)

        Args:
            session_id: Session identifier

        Returns:
            user_id if session is valid, None otherwise
        """
        session = await self.get_session(session_id)
        return session.user_id if session else None

    @trace("session_service_update_session")
    async def update_session(
        self,
        session_id: str,
        updates: Dict[str, Any],
    ) -> bool:
        """Update session metadata (authentication context only)

        Args:
            session_id: Session identifier
            updates: Updates to apply (metadata only - no case data allowed)

        Returns:
            True if update successful

        Raises:
            ValidationException: If updates contain forbidden fields
        """
        if not session_id or not session_id.strip():
            raise ValidationException("session_id cannot be empty")

        if not updates:
            raise ValidationException("updates cannot be empty")

        # Validate no case data in updates (spec compliance)
        forbidden_fields = {'case_history', 'current_case_id', 'data_uploads', 'agent_state'}
        if any(field in updates for field in forbidden_fields):
            raise ValidationException(
                f"Cannot update session with case data. Forbidden fields: {forbidden_fields}. "
                "Sessions are for authentication only per spec lines 102-107."
            )

        session = await self.get_session(session_id)
        if not session:
            return False

        # Apply updates
        for key, value in updates.items():
            if hasattr(session, key):
                setattr(session, key, value)

        session.updated_at = datetime.now(timezone.utc)

        # Persist
        if self.session_store:
            await self.session_store.save(session)

        return True

    @trace("session_service_extend_session")
    async def extend_session(self, session_id: str, extend_by_hours: int = 24) -> bool:
        """Extend session expiration

        Args:
            session_id: Session identifier
            extend_by_hours: Hours to extend session by

        Returns:
            True if extended successfully
        """
        session = await self.get_session(session_id)
        if not session:
            return False

        now = datetime.now(timezone.utc)
        session.expires_at = now + timedelta(hours=extend_by_hours)
        session.updated_at = now

        if self.session_store:
            await self.session_store.save(session)

        self.logger.debug(f"Extended session {session_id} by {extend_by_hours} hours")
        return True

    @trace("session_service_update_last_activity")
    async def update_last_activity(self, session_id: str) -> bool:
        """Update session last activity timestamp

        Args:
            session_id: Session identifier

        Returns:
            True if updated successfully
        """
        session = await self.get_session(session_id)
        if not session:
            return False

        session.last_activity = datetime.now(timezone.utc)
        session.updated_at = session.last_activity

        if self.session_store:
            await self.session_store.save(session)

        return True

    @trace("session_service_delete_session")
    async def delete_session(self, session_id: str) -> bool:
        """Delete session (logout)

        Args:
            session_id: Session identifier

        Returns:
            True if deleted successfully
        """
        if not session_id or not session_id.strip():
            return False

        if not self.session_store:
            return False

        success = await self.session_store.delete(session_id)

        if success:
            self.logger.info(f"Deleted session {session_id}")

        return success

    # =========================================================================
    # Multi-User Session Management
    # =========================================================================

    @trace("session_service_get_user_sessions")
    async def get_user_sessions(self, user_id: str) -> List[SessionContext]:
        """Get all active sessions for a user (multi-device support)

        Implements spec lines 88-92: "Multiple concurrent sessions per user"

        Args:
            user_id: User identifier

        Returns:
            List of active sessions for user
        """
        if not user_id or not user_id.strip():
            return []

        if not self.session_store:
            return []

        all_sessions = await self.session_store.list()
        user_sessions = [
            s for s in all_sessions
            if s.user_id == user_id and await self._is_active(s)
        ]

        return user_sessions

    @trace("session_service_list_sessions")
    async def list_sessions(self, user_id: Optional[str] = None) -> List[SessionContext]:
        """List sessions, optionally filtered by user

        Args:
            user_id: Optional user filter

        Returns:
            List of sessions
        """
        if not self.session_store:
            return []

        all_sessions = await self.session_store.list()

        if user_id:
            return [s for s in all_sessions if s.user_id == user_id]

        return all_sessions

    # =========================================================================
    # Session Cleanup and Maintenance
    # =========================================================================

    @trace("session_service_cleanup_expired_sessions")
    async def cleanup_expired_sessions(self) -> int:
        """Clean up expired sessions

        Returns:
            Number of sessions cleaned up
        """
        if not self.session_store:
            return 0

        all_sessions = await self.session_store.list()
        now = datetime.now(timezone.utc)
        cleaned = 0

        for session in all_sessions:
            if session.expires_at and now > session.expires_at:
                await self.delete_session(session.session_id)
                cleaned += 1

        if cleaned > 0:
            self.logger.info(f"Cleaned up {cleaned} expired sessions")

        return cleaned

    @trace("session_service_cleanup_inactive_sessions")
    async def cleanup_inactive_sessions(self) -> int:
        """Clean up inactive sessions (no activity > threshold)

        Returns:
            Number of sessions cleaned up
        """
        if not self.session_store:
            return 0

        all_sessions = await self.session_store.list()
        now = datetime.now(timezone.utc)
        cleaned = 0

        for session in all_sessions:
            time_since_activity = now - session.last_activity
            if time_since_activity > self.inactive_threshold:
                await self.delete_session(session.session_id)
                cleaned += 1

        if cleaned > 0:
            self.logger.info(f"Cleaned up {cleaned} inactive sessions")

        return cleaned

    # =========================================================================
    # Session Analytics and Monitoring
    # =========================================================================

    @trace("session_service_get_session_analytics")
    async def get_session_analytics(self) -> Dict[str, Any]:
        """Get session analytics

        Returns:
            Analytics data for all sessions
        """
        if not self.session_store:
            return {}

        all_sessions = await self.session_store.list()
        now = datetime.now(timezone.utc)

        active_sessions = [s for s in all_sessions if await self._is_active(s)]

        # Group by user to track multi-device usage
        sessions_by_user = {}
        for session in active_sessions:
            if session.user_id not in sessions_by_user:
                sessions_by_user[session.user_id] = []
            sessions_by_user[session.user_id].append(session)

        return {
            "total_sessions": len(all_sessions),
            "active_sessions": len(active_sessions),
            "unique_users": len(sessions_by_user),
            "multi_device_users": len([u for u, sessions in sessions_by_user.items() if len(sessions) > 1]),
            "average_sessions_per_user": len(active_sessions) / len(sessions_by_user) if sessions_by_user else 0,
            "timestamp": to_json_compatible(now)
        }

    @trace("session_service_get_user_session_analytics")
    async def get_user_session_analytics(self, user_id: str) -> Dict[str, Any]:
        """Get session analytics for specific user

        Args:
            user_id: User identifier

        Returns:
            Analytics data for user's sessions
        """
        user_sessions = await self.get_user_sessions(user_id)

        return {
            "user_id": user_id,
            "active_sessions": len(user_sessions),
            "devices": [s.client_id for s in user_sessions if s.client_id],
            "sessions": [
                {
                    "session_id": s.session_id,
                    "client_id": s.client_id,
                    "created_at": to_json_compatible(s.created_at),
                    "last_activity": to_json_compatible(s.last_activity),
                    "expires_at": to_json_compatible(s.expires_at) if s.expires_at else None,
                }
                for s in user_sessions
            ]
        }

    @trace("session_service_get_session_health")
    async def get_session_health(self) -> Dict[str, Any]:
        """Get session health metrics

        Returns:
            Health metrics for session system
        """
        if not self.session_store:
            return {"status": "unhealthy", "reason": "session_store not configured"}

        all_sessions = await self.session_store.list()
        now = datetime.now(timezone.utc)

        active = sum(1 for s in all_sessions if await self._is_active(s))
        expired = sum(1 for s in all_sessions if s.expires_at and now > s.expires_at)

        return {
            "status": "healthy",
            "total_sessions": len(all_sessions),
            "active_sessions": active,
            "expired_sessions": expired,
            "timestamp": to_json_compatible(now)
        }

    # =========================================================================
    # Private Helper Methods
    # =========================================================================

    async def _find_session_by_user_and_client(
        self,
        user_id: str,
        client_id: str
    ) -> Optional[SessionContext]:
        """Find existing session for (user, client) pair for resumption

        Args:
            user_id: User identifier
            client_id: Client identifier

        Returns:
            Existing session if found and valid, None otherwise
        """
        if not self.session_store:
            return None

        # Check if store has index lookup capability
        if hasattr(self.session_store, 'find_by_user_and_client'):
            session_id = await self.session_store.find_by_user_and_client(user_id, client_id)
            if session_id:
                return await self.get_session(session_id)

        # Fallback: search all sessions
        all_sessions = await self.session_store.list()
        for session in all_sessions:
            if session.user_id == user_id and session.client_id == client_id:
                if await self._is_active(session):
                    return session

        return None

    async def _enforce_session_limit(self, user_id: str) -> None:
        """Enforce max sessions per user limit

        Args:
            user_id: User identifier

        Raises:
            ServiceException: If limit exceeded and cleanup fails
        """
        user_sessions = await self.get_user_sessions(user_id)

        if len(user_sessions) >= self.max_sessions_per_user:
            # Clean up oldest session
            oldest_session = min(user_sessions, key=lambda s: s.last_activity)
            await self.delete_session(oldest_session.session_id)
            self.logger.info(
                f"Deleted oldest session for user {user_id} to enforce limit: {oldest_session.session_id}"
            )

    async def _is_active(self, session: SessionContext) -> bool:
        """Check if session is active (not expired and recent activity)

        Args:
            session: Session to check

        Returns:
            True if session is active
        """
        now = datetime.now(timezone.utc)

        # Check expiration
        if session.expires_at and now > session.expires_at:
            return False

        # Check recent activity
        time_since_activity = now - session.last_activity
        return time_since_activity < self.inactive_threshold
