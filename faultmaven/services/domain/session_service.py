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

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Union, Tuple

from faultmaven.services.base import BaseService
from faultmaven.models import AgentState, SessionContext, parse_utc_timestamp
from faultmaven.models.interfaces import ITracer
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.models.case import CaseMessage, MessageType
from faultmaven.infrastructure.observability.tracing import trace
# from faultmaven.session_management import SessionManager  # Temporarily disabled
from faultmaven.exceptions import ValidationException, ServiceException
from faultmaven.utils.serialization import to_json_compatible


class SessionService(BaseService):
    """Service for centralized session management and coordination"""

    def __init__(
        self,
        session_store: Optional[Any] = None,  # ISessionStore interface
        case_service: Optional[ICaseService] = None,
        settings: Optional[Any] = None,
        max_sessions_per_user: int = 10,
        inactive_threshold_hours: int = 24,
    ):
        """
        Initialize the Session Service

        Args:
            session_store: Session store interface for persistence
            case_service: Optional case service for case persistence features
            settings: Configuration settings for the service
            max_sessions_per_user: Maximum concurrent sessions per user
            inactive_threshold_hours: Hours before marking session inactive
        """
        super().__init__("session_service")
        self.session_store = session_store
        self.case_service = case_service
        self._settings = settings
        
        # Use settings values if available, otherwise use parameter defaults
        if settings and hasattr(settings, 'session'):
            self.max_sessions_per_user = getattr(settings.session, 'max_sessions_per_user', max_sessions_per_user)
            timeout_hours = getattr(settings.session, 'timeout_minutes', inactive_threshold_hours * 60) / 60
            self.inactive_threshold = timedelta(hours=timeout_hours)
        else:
            self.max_sessions_per_user = max_sessions_per_user
            self.inactive_threshold = timedelta(hours=inactive_threshold_hours)
        # Note: self.logger from BaseService replaces the manual logger

        # Compatibility alias for session_manager -> session_store
        self.session_manager = self.session_store

    @trace("session_service_create_session")
    async def create_session(
        self, 
        user_id: Optional[str] = None, 
        initial_context: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        client_id: Optional[str] = None
    ) -> Union[SessionContext, Tuple[SessionContext, bool]]:
        """
        Create a new session with validation and limits, or resume existing session if client_id provided

        Args:
            user_id: Optional user identifier
            initial_context: Optional initial context for the session
            metadata: Optional session metadata
            client_id: Optional client/device identifier for session resumption

        Returns:
            SessionContext or Tuple[SessionContext, bool] where bool indicates if session was resumed

        Raises:
            ValueError: If session limits are exceeded
        """
        self.logger.info(f"Creating session for user: {user_id or 'anonymous'}, client_id: {client_id or 'none'}")

        # If client_id provided, try to find and resume existing session
        if client_id and user_id and self.session_store:
            try:
                existing_session_id = await self.session_store.find_by_user_and_client(user_id, client_id)
                if existing_session_id:
                    # Try to get existing session
                    existing_session = await self.get_session(existing_session_id, validate=False)
                    if existing_session and await self._is_active(existing_session):
                        # Extract timeout from metadata for TTL calculation
                        timeout_minutes = metadata.get('timeout_minutes', 180) if metadata else 180
                        ttl_seconds = timeout_minutes * 60
                        
                        # Refresh session TTL with potentially new timeout value
                        await self.session_store.extend_ttl(existing_session_id, ttl_seconds)
                        
                        # Update last activity
                        await self.update_last_activity(existing_session_id)
                        
                        # Update client index TTL to match session TTL  
                        await self.session_store.index_session_by_client(
                            user_id, client_id, existing_session_id, 
                            ttl=ttl_seconds
                        )
                        
                        self.logger.info(f"Resumed existing session {existing_session_id} for client {client_id} with {timeout_minutes}min timeout")
                        return existing_session, True
                    else:
                        # Session expired or invalid, clean up client index
                        await self.session_store.remove_client_index(user_id, client_id)
                        if existing_session_id:
                            self.logger.warn(f"Client {client_id} attempted to resume expired session {existing_session_id}")
                        else:
                            self.logger.info(f"Cleaned up expired session index for client {client_id}")
            except Exception as e:
                self.logger.warning(f"Failed to check existing session for client {client_id}: {e}")

        # Check user session limits for new session creation
        if user_id:
            try:
                user_sessions = await self.get_user_sessions(user_id)
                active_count = len([s for s in user_sessions if await self._is_active(s)])

                if active_count >= self.max_sessions_per_user:
                    self.logger.warning(
                        f"User {user_id} has reached session limit ({self.max_sessions_per_user})"
                    )
                    # Clean up oldest inactive session
                    await self._cleanup_oldest_session(user_sessions)
            except Exception as e:
                self.logger.warning(f"Failed to check session limits for user {user_id}: {e}")

        # Create new session through manager
        try:
            session = await self.session_manager.create_session(user_id)

            # Add initial context and metadata if provided
            updates = {}
            if initial_context:
                updates.update(initial_context)
            if metadata:
                updates.update(metadata)
            
            if updates:
                update_success = await self.session_manager.update_session(session.session_id, updates)
                if not update_success:
                    self.logger.warning(f"Failed to add context/metadata to session {session.session_id}")

            # Create client index if client_id provided
            if client_id and user_id and self.session_store:
                try:
                    # Use timeout from metadata for TTL calculation  
                    timeout_minutes = metadata.get('timeout_minutes', 180) if metadata else 180
                    ttl_seconds = timeout_minutes * 60
                    await self.session_store.index_session_by_client(user_id, client_id, session.session_id, ttl_seconds)
                    self.logger.info(f"Created client index for session {session.session_id}, client {client_id} with {timeout_minutes}min TTL")
                except Exception as e:
                    self.logger.warning(f"Failed to create client index: {e}")

            self.logger.info(f"Created session {session.session_id}")
            return session, False  # False indicates new session (not resumed)
        except Exception as e:
            self.logger.error(f"Failed to create session: {e}")
            raise RuntimeError(f"Session creation failed: {str(e)}") from e

    async def cleanup_expired_sessions(self) -> int:
        """
        Clean up expired sessions and their associated data.
        
        This method should be called periodically (every 30 minutes) to remove
        expired sessions and prevent database bloat. It handles:
        - Removing expired session data
        - Cleaning up client index entries
        - Removing related case/conversation data
        
        Returns:
            Number of sessions cleaned up
        """
        cleaned_count = 0
        try:
            # Get all user sessions and check which are expired
            all_sessions = await self.session_manager.get_all_sessions()
            
            for session in all_sessions:
                if not await self._is_active(session):
                    try:
                        # Clean up the session
                        await self.delete_session(session.session_id)
                        cleaned_count += 1
                        
                        # Log cleanup for debugging
                        timeout_minutes = getattr(session, 'timeout_minutes', 'unknown')
                        self.logger.info(f"Session {session.session_id} expired after {timeout_minutes} minutes - cleaned up")
                        
                    except Exception as e:
                        self.logger.warning(f"Failed to cleanup session {session.session_id}: {e}")
            
            if cleaned_count > 0:
                self.logger.info(f"Session cleanup completed: {cleaned_count} expired sessions removed")
            
            return cleaned_count
            
        except Exception as e:
            self.logger.error(f"Session cleanup failed: {e}")
            return cleaned_count

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
            
        Raises:
            ValidationException: If session_id is invalid
            RuntimeError: If session retrieval fails
        """
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")
            
        try:
            session = await self.session_manager.get_session(session_id)

            if session and validate:
                # Validate session is still active
                if not await self._is_active(session):
                    self.logger.warning(f"Session {session_id} is inactive")
                    return None

            return session
        except ValidationException:
            # Re-raise validation exceptions without wrapping
            raise
        except Exception as e:
            self.logger.error(f"Failed to get session {session_id}: {e}")
            raise RuntimeError(f"Session retrieval failed: {str(e)}") from e

    @trace("session_service_list_sessions")
    async def list_sessions(self, user_id: Optional[str] = None) -> List[SessionContext]:
        """
        List sessions with optional user filtering

        Args:
            user_id: Optional user ID to filter by

        Returns:
            List of SessionContext objects
        """
        try:
            if user_id:
                # Get sessions for specific user
                return await self.session_manager.list_sessions(user_id=user_id)
            else:
                # Get all sessions
                return await self.session_manager.list_sessions()

        except Exception as e:
            self.logger.error(f"Failed to list sessions: {e}")
            return []

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
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")
            
        if not updates:
            raise ValidationException("Updates cannot be empty")
            
        try:
            # Get current session
            session = await self.get_session(session_id, validate=False)
            if not session:
                self.logger.error(f"Session {session_id} not found")
                raise FileNotFoundError(f"Session {session_id} not found")

            # Validate state transitions if needed
            if validate_state and "agent_state" in updates:
                if not self._validate_state_transition(
                    session.agent_state, updates["agent_state"]
                ):
                    return False  # Return False for invalid state transition

            # Apply updates
            result = await self.session_manager.update_session(session_id, updates)
            if not result:
                raise RuntimeError("Session update operation failed")
            return result

        except ValidationException:
            # Re-raise validation exceptions without wrapping
            raise
        except FileNotFoundError:
            # Re-raise file not found exceptions without wrapping
            raise
        except RuntimeError:
            # Re-raise runtime exceptions without wrapping
            raise
        except Exception as e:
            self.logger.error(f"Failed to update session: {e}")
            raise RuntimeError(f"Session update failed: {str(e)}") from e

    @trace("session_service_get_user_sessions")
    async def get_user_sessions(self, user_id: str) -> List[SessionContext]:
        """
        Get all sessions for a user

        Args:
            user_id: User identifier

        Returns:
            List of user's sessions
            
        Raises:
            ValidationException: If user_id is invalid
            RuntimeError: If session listing fails
        """
        if not user_id or not user_id.strip():
            raise ValidationException("User ID cannot be empty")
            
        try:
            return await self.session_manager.list_sessions(user_id=user_id)
        except Exception as e:
            self.logger.error(f"Failed to get sessions for user {user_id}: {e}")
            raise RuntimeError(f"Session listing failed: {str(e)}") from e

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
                duration = datetime.now(timezone.utc) - session.created_at
                durations.append(duration.total_seconds() / 3600)

            # Case analytics
            total_cases = sum(
                len(s.case_history) for s in all_sessions
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
                "total_cases": total_cases,
                "average_cases_per_session": (
                    total_cases / len(all_sessions) if all_sessions else 0
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
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")
            
        if extension_hours <= 0:
            raise ValidationException("Extension hours must be positive")
            
        try:
            # Validate session exists and is active
            session = await self.get_session(session_id, validate=False)
            if not session:
                raise FileNotFoundError(f"Session {session_id} not found")

            # Extend through manager
            result = await self.session_manager.extend_session(session_id)
            if not result:
                raise RuntimeError("Session extension operation failed")
            return result

        except ValidationException:
            # Re-raise validation exceptions without wrapping
            raise
        except FileNotFoundError:
            # Re-raise file not found exceptions without wrapping
            raise
        except RuntimeError:
            # Re-raise runtime exceptions without wrapping
            raise
        except Exception as e:
            self.logger.error(f"Failed to extend session: {e}")
            raise RuntimeError(f"Session extension failed: {str(e)}") from e

    @trace("session_service_update_last_activity")
    async def update_last_activity(self, session_id: str) -> bool:
        """
        Update the last activity timestamp for a session (heartbeat)

        Args:
            session_id: Session identifier

        Returns:
            True if activity timestamp was updated successfully
        """
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")
            
        try:
            # Validate session exists first
            session = await self.get_session(session_id, validate=False)
            if not session:
                raise FileNotFoundError(f"Session {session_id} not found")

            # Update activity through manager
            result = await self.session_manager.update_last_activity(session_id)
            if not result:
                raise RuntimeError("Activity update operation failed")
            return result

        except ValidationException:
            # Re-raise validation exceptions without wrapping
            raise
        except FileNotFoundError:
            # Re-raise file not found exceptions without wrapping
            raise
        except ConnectionError as e:
            # Handle Redis connection errors specifically
            self.logger.error(f"Session store connection error during heartbeat for {session_id}: {e}")
            raise RuntimeError(f"Session store unavailable: {str(e)}") from e
        except RuntimeError:
            # Re-raise runtime exceptions without wrapping
            raise
        except Exception as e:
            self.logger.error(f"Failed to update session activity: {e}")
            raise RuntimeError(f"Session activity update failed: {str(e)}") from e

    @trace("session_service_delete_session")
    async def delete_session(self, session_id: str) -> bool:
        """
        Delete a session
        
        Args:
            session_id: Session identifier
            
        Returns:
            True if session was deleted successfully, False otherwise
        """
        if not session_id or not session_id.strip():
            return False
            
        try:
            # Delegate to session manager
            result = await self.session_manager.delete_session(session_id)
            if result:
                self.logger.info(f"Successfully deleted session {session_id}")
            else:
                self.logger.warning(f"Failed to delete session {session_id} - session may not exist")
            return result
            
        except Exception as e:
            self.logger.error(f"Error deleting session {session_id}: {e}")
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
            duration = datetime.now(timezone.utc) - session.created_at
            is_active = await self._is_active(session)

            # Extract case summary
            case_summary = {
                "total": len(session.case_history),
                "successful": len(
                    [i for i in session.case_history 
                     if i.get("confidence_score", 0) > 0.7]
                ),
                "recent": session.case_history[-5:] if session.case_history else [],
            }

            summary = {
                "session_id": session.session_id,
                "user_id": session.user_id,
                "status": "active" if is_active else "inactive",
                "created_at": to_json_compatible(session.created_at),
                "last_activity": to_json_compatible(session.last_activity),
                "duration_hours": round(duration.total_seconds() / 3600, 2),
                "data_uploads_count": len(session.data_uploads),
                "case_summary": case_summary,
                "current_agent_state": session.agent_state,
            }

            return summary

        except Exception as e:
            self.logger.error(f"Failed to get session summary: {e}")
            return None

    async def _is_active(self, session: SessionContext) -> bool:
        """Check if a session is considered active"""
        time_since_activity = datetime.now(timezone.utc) - session.last_activity
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

            # Merge case history
            for case_item in secondary.case_history:
                await self.session_manager.add_case_history(
                    primary_session_id, case_item
                )

            # Delete secondary session
            await self.session_manager.delete_session(secondary_session_id)

            self.logger.info(f"Merged session {secondary_session_id} into {primary_session_id}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to merge sessions: {e}")
            return False

    @trace("session_service_record_query_operation")
    async def record_query_operation(
        self,
        session_id: str,
        query: str,
        case_id: str,
        context: Optional[Dict] = None,
        confidence_score: float = 0.0
    ) -> bool:
        """
        Record a query operation in the session's case history
        
        Args:
            session_id: Session identifier  
            query: The query text
            case_id: Unique case identifier
            context: Optional context information
            confidence_score: Confidence score for the query result
            
        Returns:
            True if operation was recorded successfully
        """
        try:
            case_record = {
                "action": "query_processed",
                "case_id": case_id,
                "query": query,
                "context": context or {},
                "confidence_score": confidence_score,
                "timestamp": to_json_compatible(datetime.now(timezone.utc))
            }
            
            await self.session_manager.add_case_history(session_id, case_record)
            await self.update_last_activity(session_id)
            
            self.logger.debug(f"Recorded query operation for session {session_id}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to record query operation: {e}")
            return False

    @trace("session_service_record_data_upload_operation")
    async def record_data_upload_operation(
        self,
        session_id: str,
        operation_type: str,
        data_id: str,
        context: Optional[Dict] = None,
        filename: Optional[str] = None,
        file_size: int = 0,
        metadata: Optional[Dict] = None
    ) -> bool:
        """
        Record a data upload operation in the session
        
        Args:
            session_id: Session identifier
            operation_type: Type of operation (e.g., data_ingestion, log_analysis)
            data_id: Unique data identifier
            context: Optional context information about the operation
            filename: Name of uploaded file (optional)
            file_size: Size of uploaded file in bytes
            metadata: Optional metadata about the upload
            
        Returns:
            True if operation was recorded successfully
        """
        try:
            # Add to data uploads
            await self.session_manager.add_data_upload(session_id, data_id)
            
            # Also add to case history for tracking
            upload_record = {
                "action": "data_uploaded",
                "operation_type": operation_type,
                "data_id": data_id,
                "context": context or {},
                "filename": filename,
                "file_size": file_size,
                "metadata": metadata or {},
                "timestamp": to_json_compatible(datetime.now(timezone.utc))
            }
            
            await self.session_manager.add_case_history(session_id, upload_record)
            await self.update_last_activity(session_id)
            
            self.logger.debug(f"Recorded data upload operation for session {session_id}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to record data upload operation: {e}")
            return False


    async def cleanup_session_data(self, session_id: str) -> Dict[str, Any]:
        """
        Clean up session data and temporary files
        
        Args:
            session_id: Session identifier
            
        Returns:
            Cleanup results
        """
        try:
            return await self.session_manager.cleanup_session_data(session_id)
        except Exception as e:
            self.logger.error(f"Failed to cleanup session: {e}")
            return {
                "session_id": session_id,
                "success": False,
                "error": str(e)
            }

    @trace("session_service_get_sessions_by_criteria")
    async def get_sessions_by_criteria(self, criteria: Dict[str, Any]) -> List[SessionContext]:
        """
        Get sessions matching specific criteria
        
        Args:
            criteria: Dictionary of criteria to filter sessions
            
        Returns:
            List of sessions matching criteria
        """
        try:
            all_sessions = await self.session_manager.list_sessions()
            
            # Filter sessions based on criteria
            matching_sessions = []
            for session in all_sessions:
                # Check if session matches all criteria
                matches = True
                for key, value in criteria.items():
                    # Check in agent state case context
                    if (session.agent_state and 
                        session.agent_state.get("case_context", {}).get(key) != value):
                        matches = False
                        break
                
                if matches:
                    matching_sessions.append(session)
            
            return matching_sessions
            
        except Exception as e:
            self.logger.error(f"Failed to get sessions by criteria: {e}")
            return []

    @trace("session_service_get_session_health")
    async def get_session_health(self) -> Dict[str, Any]:
        """
        Get session health status and metrics
        
        Returns:
            Health status dictionary
        """
        try:
            all_sessions = await self.session_manager.list_sessions()
            active_sessions = [s for s in all_sessions if await self._is_active(s)]
            
            # Count sessions by state
            state_distribution = {
                "initial": 0,
                "investigating": 0,
                "waiting_for_input": 0,
                "error": 0,
                "completed": 0,
                "other": 0
            }
            
            error_count = 0
            for session in all_sessions:
                if session.agent_state:
                    phase = session.agent_state.get("current_phase", "other")
                    if phase in state_distribution:
                        state_distribution[phase] += 1
                        if phase == "error":
                            error_count += 1
                    else:
                        state_distribution["other"] += 1
            
            # Calculate average session age
            now = datetime.now(timezone.utc)
            session_ages = [(now - s.created_at).total_seconds() / 3600 for s in all_sessions]
            avg_age = sum(session_ages) / len(session_ages) if session_ages else 0
            
            # Determine service status
            service_status = "healthy"
            if error_count > len(all_sessions) * 0.2:  # More than 20% error sessions
                service_status = "unhealthy"
            elif error_count > len(all_sessions) * 0.1:  # More than 10% error sessions
                service_status = "degraded"
            
            return {
                "service_status": service_status,
                "total_sessions": len(all_sessions),
                "active_sessions": len(active_sessions),
                "session_distribution": state_distribution,
                "error_sessions": error_count,
                "average_session_age": round(avg_age, 2)
            }
            
        except Exception as e:
            self.logger.error(f"Failed to get session health: {e}")
            return {
                "service_status": "unhealthy",
                "total_sessions": 0,
                "active_sessions": 0,
                "session_distribution": {},
                "error_sessions": 0,
                "average_session_age": 0
            }

    @trace("session_service_get_user_session_analytics")
    async def get_user_session_analytics(self, user_id: str) -> Dict[str, Any]:
        """
        Get analytics for a specific user's sessions
        
        Args:
            user_id: User identifier
            
        Returns:
            User session analytics dictionary
        """
        try:
            user_sessions = await self.get_user_sessions(user_id)
            
            if not user_sessions:
                return {
                    "user_id": user_id,
                    "session_count": 0,
                    "total_duration": 0,
                    "average_duration": 0,
                    "active_sessions": 0,
                    "completed_sessions": 0
                }
            
            # Calculate duration metrics
            now = datetime.now(timezone.utc)
            durations = [(now - s.created_at).total_seconds() / 3600 for s in user_sessions]
            total_duration = sum(durations)
            avg_duration = total_duration / len(durations)
            
            # Count session states
            active_count = len([s for s in user_sessions if await self._is_active(s)])
            completed_count = len([
                s for s in user_sessions 
                if s.agent_state and s.agent_state.get("current_phase") == "completed"
            ])
            
            return {
                "user_id": user_id,
                "session_count": len(user_sessions),
                "total_duration": round(total_duration, 2),
                "average_duration": round(avg_duration, 2),
                "active_sessions": active_count,
                "completed_sessions": completed_count,
                "success_rate": round(completed_count / len(user_sessions), 2) if user_sessions else 0
            }
            
        except Exception as e:
            self.logger.error(f"Failed to get user session analytics: {e}")
            return {
                "user_id": user_id,
                "session_count": 0,
                "total_duration": 0,
                "average_duration": 0,
                "active_sessions": 0,
                "completed_sessions": 0,
                "success_rate": 0
            }

    @trace("session_service_get_or_create_case")
    async def get_or_create_current_case_id(self, session_id: str, force_new_case: bool = False) -> str:
        """
        Get the current case ID for a session, or create a new one
        
        Args:
            session_id: Session identifier
            force_new_case: If True, always create a new case (start new conversation)
            
        Returns:
            Current or new case ID for the session
            
        Raises:
            ValidationException: If session_id is invalid
            RuntimeError: If case management fails
        """
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")
            
        try:
            session = await self.get_session(session_id, validate=False)
            if not session:
                raise FileNotFoundError(f"Session {session_id} not found")
            
            # If forcing new case or no current case exists, create new one
            if force_new_case or not session.current_case_id:
                import uuid
                new_case_id = str(uuid.uuid4())
                
                # Update session with new case ID
                await self.update_session(session_id, {"current_case_id": new_case_id}, validate_state=False)
                
                self.logger.info(f"{'Created new case' if force_new_case else 'Initialized case'} {new_case_id} for session {session_id}")
                return new_case_id
            
            # Return existing case ID
            self.logger.debug(f"Using existing case {session.current_case_id} for session {session_id}")
            return session.current_case_id
            
        except ValidationException:
            raise
        except FileNotFoundError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to get/create case for session {session_id}: {e}")
            raise RuntimeError(f"Case management failed: {str(e)}") from e

    @trace("session_service_start_new_case")
    async def start_new_case(self, session_id: str) -> str:
        """
        Start a new case/conversation thread for a session
        
        Args:
            session_id: Session identifier
            
        Returns:
            New case ID
            
        Raises:
            ValidationException: If session_id is invalid
            RuntimeError: If new case creation fails
        """
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")
            
        try:
            # Force creation of new case
            new_case_id = await self.get_or_create_current_case_id(session_id, force_new_case=True)
            
            # Record the case start in case history
            case_start_record = {
                "action": "new_case_started",
                "case_id": new_case_id,
                "timestamp": to_json_compatible(datetime.now(timezone.utc)),
                "details": "New conversation thread initiated"
            }
            
            await self.session_manager.add_case_history(session_id, case_start_record)
            
            self.logger.info(f"Started new case {new_case_id} for session {session_id}")
            return new_case_id
            
        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to start new case for session {session_id}: {e}")
            raise RuntimeError(f"New case creation failed: {str(e)}") from e

    @trace("session_service_get_current_case")
    async def get_current_case_id(self, session_id: str) -> Optional[str]:
        """
        Get the current case ID for a session without creating a new one
        
        Args:
            session_id: Session identifier
            
        Returns:
            Current case ID or None if no active case
        """
        if not session_id or not session_id.strip():
            return None
            
        try:
            session = await self.get_session(session_id, validate=False)
            return session.current_case_id if session else None
            
        except Exception as e:
            self.logger.warning(f"Failed to get current case for session {session_id}: {e}")
            return None

    @trace("session_service_update_case_activity")
    async def update_case_activity(self, session_id: str, case_id: str, activity_details: Dict[str, Any]) -> bool:
        """
        Record activity for the current case
        
        Args:
            session_id: Session identifier
            case_id: Case identifier
            activity_details: Details about the case activity
            
        Returns:
            True if activity was recorded successfully
        """
        try:
            # Verify the case_id matches current session case
            current_case = await self.get_current_case_id(session_id)
            if current_case != case_id:
                self.logger.warning(f"Case ID mismatch: current={current_case}, provided={case_id}")
                return False
            
            # Record activity in case history
            activity_record = {
                "action": "case_activity",
                "case_id": case_id,
                "timestamp": to_json_compatible(datetime.now(timezone.utc)),
                **activity_details
            }
            
            await self.session_manager.add_case_history(session_id, activity_record)
            await self.update_last_activity(session_id)
            
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to record case activity: {e}")
            return False

    @trace("session_service_get_case_conversation_history")
    async def get_case_conversation_history(self, session_id: str, case_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Get conversation history for a specific case
        
        Args:
            session_id: Session identifier
            case_id: Case identifier to filter history
            limit: Maximum number of history items to return (most recent first)
            
        Returns:
            List of conversation history items for the case
        """
        try:
            session = await self.get_session(session_id, validate=False)
            if not session:
                return []
            
            # Filter case history for the current case
            case_conversation_history = []
            for item in reversed(session.case_history):  # Most recent first
                # Only include items from the current case
                if item.get("case_id") == case_id:
                    
                    # Only include query-related interactions for conversation history
                    if item.get("action") in ["query_processed", "case_activity"]:
                        case_conversation_history.append(item)
                        
                        if len(case_conversation_history) >= limit:
                            break
            
            # Format for conversation context
            conversation_history = []
            for item in reversed(case_conversation_history):  # Put back in chronological order
                if item.get("action") == "query_processed":
                    conversation_history.append({
                        "type": "user_query",
                        "content": item.get("query", ""),
                        "timestamp": item.get("timestamp"),
                        "case_id": case_id
                    })
                elif item.get("action") == "case_activity" and "query" in item:
                    conversation_history.append({
                        "type": "user_query", 
                        "content": item.get("query", ""),
                        "timestamp": item.get("timestamp"),
                        "case_id": case_id
                    })
            
            self.logger.debug(f"Retrieved {len(conversation_history)} conversation items for case {case_id}")
            return conversation_history
            
        except Exception as e:
            self.logger.warning(f"Failed to get conversation history for case {case_id}: {e}")
            return []

    @trace("session_service_format_conversation_context")
    async def format_conversation_context(self, session_id: str, case_id: str, limit: int = 5) -> str:
        """
        Format conversation history for inclusion in LLM context
        
        Args:
            session_id: Session identifier
            case_id: Case identifier
            limit: Maximum number of previous interactions to include
            
        Returns:
            Formatted conversation history string for LLM context
        """
        # Prefer case service if available for better conversation context
        if self.case_service:
            try:
                # Get conversation context from case service
                context = await self.case_service.get_case_conversation_context(
                    case_id=case_id,
                    limit=limit
                )
                if context:
                    return context
            except Exception as e:
                self.logger.warning(f"Failed to get case service conversation context: {e}")
        
        # Fall back to legacy case history method
        try:
            history = await self.get_case_conversation_history(session_id, case_id, limit)
            
            if not history:
                return ""
            
            # Format as conversation context
            formatted_lines = ["Previous conversation in this troubleshooting session:"]
            
            for i, item in enumerate(history[:-1], 1):  # Exclude the current query
                timestamp = item.get("timestamp", "")
                if timestamp:
                    # Parse and format timestamp
                    try:
                        dt = parse_utc_timestamp(timestamp)
                        time_str = dt.strftime("%H:%M")
                    except:
                        time_str = ""
                else:
                    time_str = ""
                
                query = item.get("content", "").strip()
                if query:
                    formatted_lines.append(f"{i}. [{time_str}] User: {query}")
            
            if len(formatted_lines) > 1:  # More than just the header
                formatted_lines.append("")  # Add spacing
                formatted_lines.append("Current query:")
                return "\n".join(formatted_lines)
            else:
                return ""
                
        except Exception as e:
            self.logger.warning(f"Failed to format conversation context: {e}")
            return ""

    async def format_conversation_context_token_aware(
        self,
        session_id: str,
        case_id: str,
        max_tokens: int = 4000,
        enable_summarization: bool = True
    ) -> Tuple[str, Dict[str, any]]:
        """
        Format conversation history using token-based budget management.

        This method provides intelligent context management:
        1. Calculates actual token usage (not message count)
        2. Maintains running summary of older messages
        3. Includes full text of recent messages
        4. Stays within LLM token limits

        Args:
            session_id: Session identifier
            case_id: Case identifier
            max_tokens: Maximum tokens for context (default: 4000)
            enable_summarization: Whether to use LLM summarization (default: True)

        Returns:
            Tuple of (formatted_context, metadata)

        Metadata includes:
            - total_tokens: Actual tokens used
            - recent_message_count: Number of full messages
            - summary_tokens: Tokens in summary
            - truncated: Whether older messages were summarized
        """
        from faultmaven.services.agentic.management.context_manager import (
            TokenAwareContextManager,
            ContextBudget,
            ConversationSummarizer
        )

        try:
            # Get conversation history
            history = await self.get_case_conversation_history(session_id, case_id, limit=100)

            if not history:
                return "", {"total_tokens": 0, "recent_message_count": 0}

            # Get case title for additional context
            case_title = None
            if self.case_service:
                try:
                    case = await self.case_service.get_case(case_id)
                    case_title = case.get("title") if case else None
                except Exception as e:
                    self.logger.warning(f"Failed to get case title: {e}")

            # Get existing summary from case metadata
            existing_summary = None
            if self.case_service:
                try:
                    case = await self.case_service.get_case(case_id)
                    if case:
                        existing_summary = case.get("metadata", {}).get("conversation_summary")
                except Exception as e:
                    self.logger.warning(f"Failed to get existing summary: {e}")

            # Create token budget
            budget = ContextBudget(
                max_total_tokens=max_tokens,
                reserved_for_recent=int(max_tokens * 0.5),  # 50% for recent messages
                max_summary_tokens=int(max_tokens * 0.375),  # 37.5% for summary
                min_recent_messages=3  # Always include at least 3 recent turns
            )

            # Initialize summarizer (with or without LLM)
            summarizer = None
            if enable_summarization:
                # Get LLM provider from container if available
                try:
                    from faultmaven.container import container
                    llm_provider = container.get_llm_provider()
                    summarizer = ConversationSummarizer(llm_provider=llm_provider)
                except Exception as e:
                    self.logger.warning(f"LLM provider not available for summarization: {e}")
                    summarizer = ConversationSummarizer()  # Fallback to extractive
            else:
                summarizer = ConversationSummarizer()  # No LLM summarization

            # Build token-aware context
            context_manager = TokenAwareContextManager(budget=budget, summarizer=summarizer)
            formatted_context, metadata = await context_manager.build_context(
                conversation_history=history,
                existing_summary=existing_summary,
                case_title=case_title
            )

            # Update conversation summary in case metadata if it was generated
            if metadata.get("summary_tokens", 0) > 0 and self.case_service:
                try:
                    # Extract just the summary part
                    summary_lines = formatted_context.split("Previous conversation summary:\n")
                    if len(summary_lines) > 1:
                        new_summary = summary_lines[1].split("\n\nRecent conversation:")[0]
                        await self.case_service.update_case_metadata(
                            case_id=case_id,
                            metadata={"conversation_summary": new_summary}
                        )
                except Exception as e:
                    self.logger.warning(f"Failed to update conversation summary: {e}")

            self.logger.info(
                f"Token-aware context built: {metadata['total_tokens']} tokens, "
                f"{metadata['recent_message_count']} recent messages, "
                f"summary={metadata.get('truncated', False)}"
            )

            return formatted_context, metadata

        except Exception as e:
            self.logger.error(f"Failed to build token-aware context: {e}", exc_info=True)
            # Fallback to legacy method
            legacy_context = await self.format_conversation_context(session_id, case_id, limit=15)
            return legacy_context, {
                "total_tokens": len(legacy_context) // 4,  # Rough estimate
                "recent_message_count": 15,
                "fallback": True
            }

    # ==== ENHANCED CASE PERSISTENCE METHODS ====

    @trace("session_service_get_or_create_case")
    async def get_or_create_case_for_session(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        force_new_case: bool = False,
        case_title: Optional[str] = None
    ) -> Optional[str]:
        """
        Get existing case for session or create new one with case service integration
        
        Args:
            session_id: Session identifier
            user_id: Optional user identifier
            force_new_case: Force creation of new case
            case_title: Optional case title for new cases
            
        Returns:
            Case ID if case service available, None otherwise
        """
        if not self.case_service:
            self.logger.warning("Case service not available - case persistence disabled")
            return None
            
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")
            
        try:
            # Use case service to get or create case
            case_id = await self.case_service.get_or_create_case_for_session(
                session_id=session_id,
                user_id=user_id,
                force_new=force_new_case,
                title=case_title
            )
            
            # Update session with case association
            if case_id:
                session = await self.get_session(session_id, validate=False)
                if session:
                    await self.update_session(session_id, {
                        "current_case_id": case_id
                    }, validate_state=False)
                    
                    self.logger.info(f"Associated session {session_id} with case {case_id}")
                
            return case_id
            
        except Exception as e:
            self.logger.error(f"Failed to get/create case for session {session_id}: {e}")
            raise ServiceException(f"Case management failed: {str(e)}") from e

    @trace("session_service_resume_case")
    async def resume_case_in_session(
        self,
        session_id: str,
        case_id: str,
        user_id: Optional[str] = None
    ) -> bool:
        """
        Resume an existing case in this session
        
        Args:
            session_id: Session identifier
            case_id: Case identifier to resume
            user_id: Optional user identifier for access control
            
        Returns:
            True if case was resumed successfully
        """
        if not self.case_service:
            self.logger.warning("Case service not available - cannot resume case")
            return False
            
        if not session_id or not case_id:
            raise ValidationException("Session ID and Case ID are required")
            
        try:
            # Verify session exists
            session = await self.get_session(session_id, validate=False)
            if not session:
                raise FileNotFoundError(f"Session {session_id} not found")
            
            # Check case access if user provided
            if user_id and self.case_service:
                case = await self.case_service.get_case(case_id, user_id)
                if not case:
                    self.logger.warning(f"User {user_id} cannot access case {case_id}")
                    return False
            
            # Resume case in case service
            success = await self.case_service.resume_case_in_session(case_id, session_id)
            
            if success:
                # Update session with case reference
                await self.update_session(session_id, {
                    "current_case_id": case_id
                }, validate_state=False)
                
                self.logger.info(f"Resumed case {case_id} in session {session_id}")
            
            return success
            
        except ValidationException:
            raise
        except FileNotFoundError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to resume case {case_id} in session {session_id}: {e}")
            raise ServiceException(f"Case resume failed: {str(e)}") from e

    @trace("session_service_record_case_message")
    async def record_case_message(
        self,
        session_id: str,
        message_content: str,
        message_type: MessageType = MessageType.USER_QUERY,
        author_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Record a message in the current case for this session
        
        Args:
            session_id: Session identifier
            message_content: Message content
            message_type: Type of message
            author_id: Optional message author
            metadata: Optional message metadata
            
        Returns:
            True if message was recorded successfully
        """
        if not self.case_service:
            # Fall back to legacy case history if no case service
            return await self.record_query_operation(
                session_id=session_id,
                query=message_content,
                case_id="legacy",
                context=metadata or {}
            )
            
        if not session_id or not message_content:
            raise ValidationException("Session ID and message content are required")
            
        try:
            # Get current case for session
            session = await self.get_session(session_id, validate=False)
            if not session:
                raise FileNotFoundError(f"Session {session_id} not found")
            
            case_id = session.current_case_id
            
            # Create case if none exists
            if not case_id:
                case_id = await self.get_or_create_case_for_session(
                    session_id=session_id,
                    user_id=author_id
                )
                
                if not case_id:
                    self.logger.warning(f"Could not create case for session {session_id}")
                    return False
            
            # Create message
            message = CaseMessage(
                case_id=case_id,
                session_id=session_id,
                author_id=author_id,
                message_type=message_type,
                content=message_content,
                metadata=metadata or {}
            )
            
            # Add message to case
            success = await self.case_service.add_message_to_case(
                case_id=case_id,
                message=message,
                session_id=session_id
            )
            
            if success:
                # Update session activity
                await self.update_last_activity(session_id)
                self.logger.debug(f"Recorded case message for session {session_id}")
            
            return success
            
        except ValidationException:
            raise
        except FileNotFoundError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to record case message for session {session_id}: {e}")
            return False

    @trace("session_service_get_case_conversation_context")
    async def get_case_conversation_context(
        self,
        session_id: str,
        limit: int = 10
    ) -> str:
        """
        Get conversation context from the current case for LLM processing
        
        Args:
            session_id: Session identifier
            limit: Maximum number of conversation items to include
            
        Returns:
            Formatted conversation context string
        """
        if not self.case_service:
            # Fall back to legacy conversation context
            try:
                session = await self.get_session(session_id, validate=False)
                if session and session.current_case_id:
                    return await self.format_conversation_context(
                        session_id, session.current_case_id, limit
                    )
            except:
                pass
            return ""
            
        try:
            session = await self.get_session(session_id, validate=False)
            if not session or not session.current_case_id:
                return ""
            
            # Get conversation context from case service
            context = await self.case_service.get_case_conversation_context(
                case_id=session.current_case_id,
                limit=limit
            )
            
            return context
            
        except Exception as e:
            self.logger.warning(f"Failed to get case conversation context: {e}")
            return ""

    @trace("session_service_start_new_case")
    async def start_new_case_for_session(
        self,
        session_id: str,
        case_title: Optional[str] = None,
        user_id: Optional[str] = None
    ) -> Optional[str]:
        """
        Start a new case for the session (force new conversation)
        
        Args:
            session_id: Session identifier
            case_title: Optional case title
            user_id: Optional user identifier
            
        Returns:
            New case ID if successful, None otherwise
        """
        if not self.case_service:
            self.logger.warning("Case service not available - cannot start new case")
            return None
            
        try:
            # Force creation of new case
            case_id = await self.get_or_create_case_for_session(
                session_id=session_id,
                user_id=user_id,
                force_new_case=True,
                case_title=case_title
            )
            
            if case_id:
                # Record case start event
                await self.record_case_message(
                    session_id=session_id,
                    message_content="New troubleshooting case started",
                    message_type=MessageType.SYSTEM_EVENT,
                    author_id=user_id,
                    metadata={"event": "case_started"}
                )
                
                self.logger.info(f"Started new case {case_id} for session {session_id}")
            
            return case_id
            
        except Exception as e:
            self.logger.error(f"Failed to start new case for session {session_id}: {e}")
            return None

    @trace("session_service_get_session_cases")
    async def get_session_cases(self, session_id: str) -> List[str]:
        """
        Get all cases associated with this session
        
        Args:
            session_id: Session identifier
            
        Returns:
            List of case IDs associated with this session
        """
        if not self.case_service:
            return []
            
        try:
            session = await self.get_session(session_id, validate=False)
            if not session:
                return []
            
            # Return current case if available
            if session.current_case_id:
                return [session.current_case_id]
            
            return []
            
        except Exception as e:
            self.logger.warning(f"Failed to get cases for session {session_id}: {e}")
            return []

    @trace("session_service_share_current_case")
    async def share_current_case(
        self,
        session_id: str,
        target_user_id: str,
        role: str = "viewer",
        sharer_user_id: Optional[str] = None
    ) -> bool:
        """
        Share the current case for this session with another user
        
        Args:
            session_id: Session identifier
            target_user_id: User to share case with
            role: Role to assign ("viewer", "collaborator", "support")
            sharer_user_id: User performing the share action
            
        Returns:
            True if case was shared successfully
        """
        if not self.case_service:
            self.logger.warning("Case service not available - cannot share case")
            return False
            
        try:
            session = await self.get_session(session_id, validate=False)
            if not session or not session.current_case_id:
                self.logger.warning(f"No current case for session {session_id}")
                return False
            
            # Map string role to enum
            from faultmaven.models.case import ParticipantRole
            role_mapping = {
                "viewer": ParticipantRole.VIEWER,
                "collaborator": ParticipantRole.COLLABORATOR,
                "support": ParticipantRole.SUPPORT
            }
            
            participant_role = role_mapping.get(role.lower(), ParticipantRole.VIEWER)
            
            # Share case through case service
            success = await self.case_service.share_case(
                case_id=session.current_case_id,
                target_user_id=target_user_id,
                role=participant_role,
                sharer_user_id=sharer_user_id
            )
            
            if success:
                # Record share event
                await self.record_case_message(
                    session_id=session_id,
                    message_content=f"Case shared with user {target_user_id} as {role}",
                    message_type=MessageType.SYSTEM_EVENT,
                    author_id=sharer_user_id,
                    metadata={
                        "event": "case_shared",
                        "target_user": target_user_id,
                        "role": role
                    }
                )
                
                self.logger.info(f"Shared case {session.current_case_id} with user {target_user_id}")
            
            return success
            
        except Exception as e:
            self.logger.error(f"Failed to share case for session {session_id}: {e}")
            return False

    async def get_enhanced_session_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Get enhanced session summary including case information
        
        Args:
            session_id: Session identifier
            
        Returns:
            Enhanced session summary with case details
        """
        try:
            # Get base session summary
            base_summary = await self.get_session_summary(session_id)
            if not base_summary:
                return None
            
            # Add case information if case service available
            if self.case_service:
                session = await self.get_session(session_id, validate=False)
                if session and session.current_case_id:
                    try:
                        case_analytics = await self.case_service.get_case_analytics(session.current_case_id)
                        base_summary["current_case"] = {
                            "case_id": session.current_case_id,
                            "analytics": case_analytics
                        }
                    except Exception as e:
                        self.logger.warning(f"Failed to get case analytics: {e}")
                        base_summary["current_case"] = {
                            "case_id": session.current_case_id,
                            "analytics": {}
                        }
                else:
                    base_summary["current_case"] = None
            else:
                base_summary["current_case"] = None
            
            return base_summary
            
        except Exception as e:
            self.logger.error(f"Failed to get enhanced session summary: {e}")
            return None