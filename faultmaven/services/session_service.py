"""Session Service Module

Purpose: Centralized session management service

This service provides a higher-level abstraction over the SessionManager,
adding business logic, validation, and coordination with other services.

Core Responsibilities:
- Session lifecycle management
- Session state coordination
- Session analytics and monitoring
- Session cleanup and maintenance
- Cross-service session operations
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from faultmaven.services.base_service import BaseService
from faultmaven.models import AgentState, SessionContext
from faultmaven.models.interfaces import ITracer
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.session_management import SessionManager


class SessionService(BaseService):
    """Service for centralized session management and coordination"""

    def __init__(
        self,
        session_manager: SessionManager,
        max_sessions_per_user: int = 10,
        inactive_threshold_hours: int = 24,
    ):
        """
        Initialize the Session Service

        Args:
            session_manager: Core session manager instance
            max_sessions_per_user: Maximum concurrent sessions per user
            inactive_threshold_hours: Hours before marking session inactive
        """
        super().__init__("session_service")
        self.session_manager = session_manager
        self.max_sessions_per_user = max_sessions_per_user
        self.inactive_threshold = timedelta(hours=inactive_threshold_hours)
        # Note: self.logger from BaseService replaces the manual logger

    @trace("session_service_create_session")
    async def create_session(
        self, user_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None
    ) -> SessionContext:
        """
        Create a new session with validation and limits

        Args:
            user_id: Optional user identifier
            metadata: Optional session metadata

        Returns:
            New SessionContext

        Raises:
            ValueError: If session limits are exceeded
        """
        self.logger.info(f"Creating session for user: {user_id or 'anonymous'}")

        # Check user session limits
        if user_id:
            user_sessions = await self.get_user_sessions(user_id)
            active_count = len([s for s in user_sessions if await self._is_active(s)])

            if active_count >= self.max_sessions_per_user:
                self.logger.warning(
                    f"User {user_id} has reached session limit ({self.max_sessions_per_user})"
                )
                # Clean up oldest inactive session
                await self._cleanup_oldest_session(user_sessions)

        # Create session through manager
        session = await self.session_manager.create_session(user_id)

        # Add metadata if provided
        if metadata:
            await self.session_manager.update_session(session.session_id, metadata)

        self.logger.info(f"Created session {session.session_id}")
        return session

    @trace("session_service_get_session")
    async def get_session(
        self, session_id: str, validate: bool = True
    ) -> Optional[SessionContext]:
        """
        Get session with optional validation

        Args:
            session_id: Session identifier
            validate: Whether to validate session state

        Returns:
            SessionContext or None if not found/invalid
        """
        session = await self.session_manager.get_session(session_id)

        if session and validate:
            # Validate session is still active
            if not await self._is_active(session):
                self.logger.warning(f"Session {session_id} is inactive")
                return None

        return session

    @trace("session_service_update_session")
    async def update_session(
        self, session_id: str, updates: Dict[str, Any], validate_state: bool = True
    ) -> bool:
        """
        Update session with validation

        Args:
            session_id: Session identifier
            updates: Updates to apply
            validate_state: Whether to validate state transitions

        Returns:
            True if update was successful
        """
        try:
            # Get current session
            session = await self.get_session(session_id)
            if not session:
                self.logger.error(f"Session {session_id} not found")
                return False

            # Validate state transitions if needed
            if validate_state and "agent_state" in updates:
                if not self._validate_state_transition(
                    session.agent_state, updates["agent_state"]
                ):
                    self.logger.error("Invalid state transition")
                    return False

            # Apply updates
            return await self.session_manager.update_session(session_id, updates)

        except Exception as e:
            self.logger.error(f"Failed to update session: {e}")
            return False

    @trace("session_service_get_user_sessions")
    async def get_user_sessions(self, user_id: str) -> List[SessionContext]:
        """
        Get all sessions for a user

        Args:
            user_id: User identifier

        Returns:
            List of user's sessions
        """
        return await self.session_manager.list_sessions(user_id=user_id)

    @trace("session_service_cleanup_inactive")
    async def cleanup_inactive_sessions(self) -> int:
        """
        Clean up inactive sessions across all users

        Returns:
            Number of sessions cleaned up
        """
        self.logger.info("Starting inactive session cleanup")

        try:
            all_sessions = await self.session_manager.list_sessions()
            cleanup_count = 0

            for session in all_sessions:
                if not await self._is_active(session):
                    if await self.session_manager.delete_session(session.session_id):
                        cleanup_count += 1
                        self.logger.debug(f"Cleaned up session {session.session_id}")

            self.logger.info(f"Cleaned up {cleanup_count} inactive sessions")
            return cleanup_count

        except Exception as e:
            self.logger.error(f"Cleanup failed: {e}")
            return 0

    @trace("session_service_get_analytics")
    async def get_session_analytics(self) -> Dict[str, Any]:
        """
        Get detailed session analytics

        Returns:
            Analytics dictionary
        """
        try:
            stats = await self.session_manager.get_session_stats()
            all_sessions = await self.session_manager.list_sessions()

            # Calculate additional metrics
            active_sessions = [s for s in all_sessions if await self._is_active(s)]
            
            # Session duration analytics
            durations = []
            for session in all_sessions:
                duration = datetime.utcnow() - session.created_at
                durations.append(duration.total_seconds() / 3600)

            # Investigation analytics
            total_investigations = sum(
                len(s.investigation_history) for s in all_sessions
            )
            
            # Data upload analytics
            total_uploads = sum(len(s.data_uploads) for s in all_sessions)

            analytics = {
                **stats,
                "active_session_count": len(active_sessions),
                "inactive_session_count": len(all_sessions) - len(active_sessions),
                "average_session_duration_hours": (
                    sum(durations) / len(durations) if durations else 0
                ),
                "max_session_duration_hours": max(durations) if durations else 0,
                "total_investigations": total_investigations,
                "average_investigations_per_session": (
                    total_investigations / len(all_sessions) if all_sessions else 0
                ),
                "total_data_uploads": total_uploads,
                "sessions_with_data": len([s for s in all_sessions if s.data_uploads]),
            }

            return analytics

        except Exception as e:
            self.logger.error(f"Failed to get analytics: {e}")
            raise

    @trace("session_service_extend_session")
    async def extend_session(
        self, session_id: str, extension_hours: int = 24
    ) -> bool:
        """
        Extend a session's timeout

        Args:
            session_id: Session identifier
            extension_hours: Hours to extend by

        Returns:
            True if extension was successful
        """
        try:
            # Validate session exists and is active
            session = await self.get_session(session_id)
            if not session:
                return False

            # Extend through manager
            return await self.session_manager.extend_session(session_id)

        except Exception as e:
            self.logger.error(f"Failed to extend session: {e}")
            return False

    @trace("session_service_get_session_summary")
    async def get_session_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a comprehensive summary of a session

        Args:
            session_id: Session identifier

        Returns:
            Session summary dictionary or None
        """
        try:
            session = await self.get_session(session_id, validate=False)
            if not session:
                return None

            # Calculate session metrics
            duration = datetime.utcnow() - session.created_at
            is_active = await self._is_active(session)

            # Extract investigation summary
            investigation_summary = {
                "total": len(session.investigation_history),
                "successful": len(
                    [i for i in session.investigation_history 
                     if i.get("confidence_score", 0) > 0.7]
                ),
                "recent": session.investigation_history[-5:] if session.investigation_history else [],
            }

            summary = {
                "session_id": session.session_id,
                "user_id": session.user_id,
                "status": "active" if is_active else "inactive",
                "created_at": session.created_at.isoformat(),
                "last_activity": session.last_activity.isoformat(),
                "duration_hours": duration.total_seconds() / 3600,
                "data_uploads_count": len(session.data_uploads),
                "investigation_summary": investigation_summary,
                "current_agent_state": session.agent_state,
            }

            return summary

        except Exception as e:
            self.logger.error(f"Failed to get session summary: {e}")
            return None

    async def _is_active(self, session: SessionContext) -> bool:
        """Check if a session is considered active"""
        time_since_activity = datetime.utcnow() - session.last_activity
        return time_since_activity < self.inactive_threshold

    async def _cleanup_oldest_session(self, sessions: List[SessionContext]) -> bool:
        """Clean up the oldest inactive session"""
        # Sort by last activity
        sorted_sessions = sorted(sessions, key=lambda s: s.last_activity)

        for session in sorted_sessions:
            if not await self._is_active(session):
                return await self.session_manager.delete_session(session.session_id)

        return False

    def _validate_state_transition(
        self, current_state: Optional[AgentState], new_state: AgentState
    ) -> bool:
        """Validate agent state transitions"""
        # Define valid state transitions
        valid_transitions = {
            None: ["initial", "investigating"],
            "initial": ["investigating", "analyzing"],
            "investigating": ["analyzing", "concluding", "error"],
            "analyzing": ["concluding", "investigating", "error"],
            "concluding": ["completed"],
            "error": ["investigating", "completed"],
            "completed": [],
        }

        current_phase = current_state.get("current_phase") if current_state else None
        new_phase = new_state.get("current_phase")

        # Check if transition is valid
        if current_phase in valid_transitions:
            return new_phase in valid_transitions[current_phase]

        return True  # Allow transition if current phase unknown

    @trace("session_service_merge_sessions")
    async def merge_sessions(
        self, primary_session_id: str, secondary_session_id: str
    ) -> bool:
        """
        Merge two sessions together

        Args:
            primary_session_id: Primary session to keep
            secondary_session_id: Secondary session to merge and delete

        Returns:
            True if merge was successful
        """
        try:
            primary = await self.get_session(primary_session_id)
            secondary = await self.get_session(secondary_session_id)

            if not primary or not secondary:
                return False

            # Merge data uploads
            for data_id in secondary.data_uploads:
                if data_id not in primary.data_uploads:
                    await self.session_manager.add_data_upload(primary_session_id, data_id)

            # Merge investigation history
            for investigation in secondary.investigation_history:
                await self.session_manager.add_investigation_history(
                    primary_session_id, investigation
                )

            # Delete secondary session
            await self.session_manager.delete_session(secondary_session_id)

            self.logger.info(f"Merged session {secondary_session_id} into {primary_session_id}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to merge sessions: {e}")
            return False