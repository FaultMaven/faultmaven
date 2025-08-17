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

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Union
import time

import redis.asyncio as redis

from .infrastructure.redis_client import create_redis_client, validate_redis_connection
from .models import AgentState, SessionContext, parse_utc_timestamp
from .models.interfaces import ISessionStore
from .infrastructure.persistence.redis_session_store import RedisSessionStore
from .exceptions import SessionStoreException, SessionCleanupException


class SessionManager:
    """Session manager using ISessionStore interface"""
    
    def __init__(self, session_store: Optional[ISessionStore] = None):
        """Initialize with session store interface"""
        self.logger = logging.getLogger(__name__)
        self.session_store = session_store or RedisSessionStore()
        
        # Configuration - using new ConfigurationManager
        from .config.configuration_manager import get_config
        config = get_config()
        session_config = config.get_session_config()
        
        self.session_timeout_hours = 24  # Default timeout (legacy)
        self.session_timeout_minutes = session_config["timeout_minutes"]
        self.cleanup_interval_minutes = session_config["cleanup_interval_minutes"]
        self.max_memory_mb = session_config["max_memory_mb"]
        self.cleanup_batch_size = session_config["cleanup_batch_size"]
        
        # Background cleanup task management
        self._cleanup_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        
        # Session metrics tracking
        self._cleanup_runs = 0
        self._last_cleanup_time: Optional[datetime] = None
        self._session_durations: List[float] = []  # Track session lifetimes for averaging
        
        # In-memory session index for testing/development
        self._session_index = {}  # session_id -> session metadata
        
        self.logger.info("SessionManager initialized with ISessionStore interface")
    
    async def validate_connection(self) -> bool:
        """
        Validate session store connection health.
        
        Returns:
            True if session store connection is healthy
        """
        try:
            # Try a simple operation to validate the connection
            test_key = "health_check_test"
            await self.session_store.set(test_key, {"test": "data"}, ttl=1)
            await self.session_store.delete(test_key)
            return True
        except Exception as e:
            self.logger.error(f"Session store connection validation failed: {e}")
            return False

    async def create_session(self, user_id: Optional[str] = None) -> SessionContext:
        """Create new session using interface"""
        session_id = str(uuid.uuid4())
        created_at = datetime.utcnow()
        session_data = {
            'session_id': session_id,
            'user_id': user_id,
            'created_at': created_at.isoformat() + 'Z',
            'last_activity': created_at.isoformat() + 'Z',
            'data_uploads': [],
            'case_history': [],
            'current_case_id': None
        }
        
        ttl = self.session_timeout_hours * 3600  # Convert hours to seconds
        await self.session_store.set(session_id, session_data, ttl=ttl)
        
        # Update session index for listing purposes
        self._session_index[session_id] = {
            'user_id': user_id,
            'created_at': created_at,
            'last_activity': created_at
        }
        
        # Convert back to SessionContext object
        session = SessionContext(
            session_id=session_id,
            user_id=user_id,
            created_at=created_at,
            last_activity=created_at,
            current_case_id=None,
            agent_state=None,
        )
        
        self.logger.info(f"Created new session: {session_id}")
        return session

    async def get_session(self, session_id: str) -> Optional[SessionContext]:
        """Get session using interface"""
        session_data = await self.session_store.get(session_id)
        if session_data:
            try:
                # Convert ISO strings back to datetime objects (timezone-naive for consistency)
                created_at = parse_utc_timestamp(session_data.get('created_at', datetime.utcnow().isoformat() + 'Z'))
                last_activity = parse_utc_timestamp(session_data.get('last_activity', datetime.utcnow().isoformat() + 'Z'))
                
                session = SessionContext(
                    session_id=session_data['session_id'],
                    user_id=session_data.get('user_id'),
                    created_at=created_at,
                    last_activity=last_activity,
                    current_case_id=session_data.get('current_case_id'),
                    agent_state=session_data.get('agent_state'),
                    data_uploads=session_data.get('data_uploads', []),
                    case_history=session_data.get('case_history', [])
                )
                
                # Update last activity and extend TTL
                updated_data = session_data.copy()
                updated_data['last_activity'] = datetime.utcnow().isoformat() + 'Z'
                ttl = self.session_timeout_hours * 3600
                await self.session_store.set(session_id, updated_data, ttl=ttl)
                
                self.logger.debug(f"Retrieved session: {session_id}")
                return session
                
            except (KeyError, ValueError) as e:
                self.logger.error(f"Failed to deserialize session {session_id}: {e}")
                # Clean up corrupted session
                await self.session_store.delete(session_id)
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
            # Get existing session data and update it
            session_data = await self.session_store.get(session_id)
            if not session_data:
                return False
            
            # Update session fields
            for key, value in updates.items():
                session_data[key] = value
            
            # Update last activity
            session_data['last_activity'] = datetime.utcnow().isoformat() + 'Z'

            # Save back to session store
            ttl = self.session_timeout_hours * 3600
            await self.session_store.set(session_id, session_data, ttl=ttl)

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

        # Get session data
        session_data = await self.session_store.get(session_id)
        if not session_data:
            return False
            
        data_uploads = session_data.get('data_uploads', [])
        if data_id not in data_uploads:
            data_uploads.append(data_id)
            session_data['data_uploads'] = data_uploads
            session_data['last_activity'] = datetime.utcnow().isoformat() + 'Z'

            # Save back to session store
            ttl = self.session_timeout_hours * 3600
            await self.session_store.set(session_id, session_data, ttl=ttl)

            self.logger.debug(f"Added data upload {data_id} to session {session_id}")

        return True

    async def add_case_history(
        self, session_id: str, case_data: Dict
    ) -> bool:
        """
        Add case history to a session

        Args:
            session_id: Session identifier
            case_data: Case data to add

        Returns:
            True if addition was successful
        """
        session = await self.get_session(session_id)

        if not session:
            self.logger.warning(
                f"Attempted to add history to non-existent session: {session_id}"
            )
            return False

        # Get session data and update it
        session_data = await self.session_store.get(session_id)
        if not session_data:
            return False
            
        case_data["timestamp"] = datetime.utcnow().isoformat() + 'Z'
        case_history = session_data.get('case_history', [])
        case_history.append(case_data)
        session_data['case_history'] = case_history
        session_data['last_activity'] = datetime.utcnow().isoformat() + 'Z'

        # Save back to session store
        ttl = self.session_timeout_hours * 3600
        await self.session_store.set(session_id, session_data, ttl=ttl)

        self.logger.debug(f"Added case history to session {session_id}")
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

        # Get session data and update it
        session_data = await self.session_store.get(session_id)
        if not session_data:
            return False
            
        session_data['agent_state'] = agent_state.dict() if agent_state else None
        session_data['last_activity'] = datetime.utcnow().isoformat() + 'Z'

        # Save back to session store
        ttl = self.session_timeout_hours * 3600
        await self.session_store.set(session_id, session_data, ttl=ttl)

        self.logger.debug(f"Updated agent state for session {session_id}")
        return True

    async def delete_session(self, session_id: str) -> bool:
        """
        Delete a session using interface

        Args:
            session_id: Session identifier

        Returns:
            True if session was deleted successfully
        """
        try:
            result = await self.session_store.delete(session_id)
            
            # Remove from session index
            if session_id in self._session_index:
                del self._session_index[session_id]
            
            if result:
                self.logger.info(f"Deleted session: {session_id}")
            else:
                self.logger.warning(f"Session not found for deletion: {session_id}")
            return result
        except Exception as e:
            self.logger.error(f"Failed to delete session {session_id}: {e}")
            return False

    async def list_sessions(
        self, user_id: Optional[str] = None, pattern: str = "session:*"
    ) -> List[SessionContext]:
        """
        List sessions using in-memory index

        Args:
            user_id: Optional user ID to filter sessions by
            pattern: Redis key pattern (not used in this implementation)

        Returns:
            List of active sessions
        """
        try:
            sessions = []
            
            # Clean up expired sessions from index
            current_time = datetime.utcnow()
            expired_sessions = []
            
            for session_id, session_info in self._session_index.items():
                # Check if session is expired (more than timeout hours old)
                time_diff = current_time - session_info['last_activity']
                if time_diff.total_seconds() > (self.session_timeout_hours * 3600):
                    expired_sessions.append(session_id)
                    continue
                
                # Filter by user_id if specified
                if user_id and session_info.get('user_id') != user_id:
                    continue
                
                # Try to get full session data
                session = await self.get_session(session_id)
                if session:
                    sessions.append(session)
                else:
                    # Session not found in store, mark for cleanup
                    expired_sessions.append(session_id)
            
            # Clean up expired sessions from index
            for expired_id in expired_sessions:
                if expired_id in self._session_index:
                    del self._session_index[expired_id]
                    self.logger.debug(f"Cleaned up expired session from index: {expired_id}")
                    
            self.logger.info(f"Listed {len(sessions)} sessions (filtered by user_id: {user_id})")
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
        return await self.session_store.exists(session_id)

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

        # Get session data and extend TTL
        session_data = await self.session_store.get(session_id)
        if not session_data:
            return False
            
        session_data['last_activity'] = datetime.utcnow().isoformat() + 'Z'

        # Save back to session store with extended TTL
        ttl = self.session_timeout_hours * 3600
        await self.session_store.set(session_id, session_data, ttl=ttl)

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

            # Get session data and update last activity
            session_data = await self.session_store.get(session_id)
            if not session_data:
                return False
            
            current_time = datetime.utcnow()    
            session_data['last_activity'] = current_time.isoformat() + 'Z'

            # Store updated session
            ttl = self.session_timeout_hours * 3600
            await self.session_store.set(session_id, session_data, ttl=ttl)
            
            # Update session index
            if session_id in self._session_index:
                self._session_index[session_id]['last_activity'] = current_time

            self.logger.info(f"Updated last activity for session: {session_id}")
            return True

        except Exception as e:
            self.logger.error(
                f"Failed to update last activity for session {session_id}: {e}"
            )
            return False

    async def cleanup_session_data(self, session_id: str) -> Dict[str, Any]:
        """
        Clean up session data and temporary files
        
        Args:
            session_id: Session identifier
            
        Returns:
            Dict with cleanup results
        """
        try:
            session_data = await self.session_store.get(session_id)
            if not session_data:
                return {
                    "session_id": session_id,
                    "success": False,
                    "error": "Session not found"
                }
            
            # Count items to be cleaned
            data_uploads_count = len(session_data.get('data_uploads', []))
            case_history_count = len(session_data.get('case_history', []))
            
            # Clear session data but keep the session active
            cleaned_data = {
                'session_id': session_id,
                'user_id': session_data.get('user_id'),
                'created_at': session_data.get('created_at'),
                'last_activity': datetime.utcnow().isoformat() + 'Z',
                'data_uploads': [],
                'case_history': [],
                'agent_state': None
            }
            
            # Save cleaned session
            ttl = self.session_timeout_hours * 3600
            await self.session_store.set(session_id, cleaned_data, ttl=ttl)
            
            self.logger.info(f"Cleaned up session data for {session_id}")
            
            return {
                "session_id": session_id,
                "success": True,
                "status": "cleaned",
                "message": "Session data cleaned successfully",
                "cleaned_items": {
                    "data_uploads": data_uploads_count,
                    "case_history": case_history_count,
                    "temp_files": 0  # Simulated - would clean actual temp files in production
                }
            }
            
        except Exception as e:
            self.logger.error(f"Failed to cleanup session {session_id}: {e}")
            return {
                "session_id": session_id,
                "success": False,
                "error": str(e)
            }

    async def cleanup_inactive_sessions(self, max_age_minutes: Optional[int] = None) -> int:
        """Clean up sessions that have exceeded their TTL.
        
        Args:
            max_age_minutes: Maximum session age in minutes. 
                            Defaults to SESSION_TIMEOUT_MINUTES from config.
                            
        Returns:
            Number of sessions successfully cleaned up
            
        Raises:
            SessionStoreException: If cleanup operation fails
            
        Implementation Notes:
            - Must handle concurrent access safely
            - Should batch operations for performance
            - Must log cleanup activities for auditing
            - Should not fail if individual session cleanup fails
        """
        if max_age_minutes is None:
            max_age_minutes = self.session_timeout_minutes
            
        start_time = time.time()
        cleaned_count = 0
        error_count = 0
        memory_freed_estimate = 0
        
        try:
            self.logger.info(f"Starting session cleanup (max_age: {max_age_minutes} minutes)")
            
            # Get current time for comparison
            current_time = datetime.utcnow()
            cutoff_time = current_time - timedelta(minutes=max_age_minutes)
            
            # Track sessions to clean up in batches
            sessions_to_cleanup = []
            
            # First, identify expired sessions from our index
            expired_sessions = []
            for session_id, session_info in list(self._session_index.items()):
                if session_info.get('last_activity', session_info.get('created_at')) < cutoff_time:
                    expired_sessions.append(session_id)
            
            self.logger.debug(f"Found {len(expired_sessions)} expired sessions in index")
            
            # Process sessions in batches to avoid overwhelming the system
            batch_size = min(self.cleanup_batch_size, len(expired_sessions))
            
            for i in range(0, len(expired_sessions), batch_size):
                batch = expired_sessions[i:i + batch_size]
                
                for session_id in batch:
                    try:
                        # Verify session exists in store and check its actual age
                        session_data = await self.session_store.get(session_id)
                        
                        if session_data:
                            # Parse last activity time
                            last_activity_str = session_data.get('last_activity', session_data.get('created_at'))
                            if last_activity_str:
                                try:
                                    last_activity = parse_utc_timestamp(last_activity_str)
                                    
                                    # Double-check if session is actually expired
                                    if last_activity < cutoff_time:
                                        # Calculate session duration for metrics
                                        created_at_str = session_data.get('created_at')
                                        if created_at_str:
                                            created_at = parse_utc_timestamp(created_at_str)
                                            duration_hours = (last_activity - created_at).total_seconds() / 3600
                                            self._session_durations.append(duration_hours)
                                        
                                        # Delete from store
                                        if await self.session_store.delete(session_id):
                                            cleaned_count += 1
                                            # Estimate memory freed (rough calculation)
                                            memory_freed_estimate += len(str(session_data)) / 1024  # KB
                                            
                                            self.logger.debug(f"Cleaned up expired session: {session_id}")
                                        else:
                                            error_count += 1
                                            self.logger.warning(f"Failed to delete session from store: {session_id}")
                                    else:
                                        # Session was updated since index check, keep it
                                        self.logger.debug(f"Session {session_id} was recently updated, keeping")
                                        
                                except ValueError as e:
                                    error_count += 1
                                    self.logger.warning(f"Invalid timestamp in session {session_id}: {e}")
                                    # Clean up corrupted session
                                    await self.session_store.delete(session_id)
                                    cleaned_count += 1
                            else:
                                error_count += 1
                                self.logger.warning(f"Session {session_id} missing timestamp, cleaning up")
                                await self.session_store.delete(session_id)
                                cleaned_count += 1
                        
                        # Remove from index regardless
                        if session_id in self._session_index:
                            del self._session_index[session_id]
                            
                    except Exception as e:
                        error_count += 1
                        self.logger.error(f"Error cleaning session {session_id}: {e}")
                        # Continue with next session
                        continue
                
                # Small delay between batches to avoid overwhelming the system
                if i + batch_size < len(expired_sessions):
                    await asyncio.sleep(0.1)
            
            # Update metrics
            self._cleanup_runs += 1
            self._last_cleanup_time = current_time
            duration_ms = round((time.time() - start_time) * 1000, 2)
            
            # Log structured cleanup results
            self.logger.info(
                "Session cleanup completed",
                extra={
                    "cleanup_count": cleaned_count,
                    "duration_ms": duration_ms,
                    "memory_freed_kb": round(memory_freed_estimate, 2),
                    "errors_encountered": error_count,
                    "batch_size": batch_size,
                    "total_batches": len(expired_sessions) // batch_size + (1 if len(expired_sessions) % batch_size > 0 else 0)
                }
            )
            
            return cleaned_count
            
        except Exception as e:
            duration_ms = round((time.time() - start_time) * 1000, 2)
            self.logger.error(f"Session cleanup failed after {duration_ms}ms: {e}")
            raise SessionCleanupException(f"Cleanup operation failed: {e}", {"duration_ms": duration_ms, "cleaned_count": cleaned_count})

    async def start_cleanup_scheduler(self, interval_minutes: int = 15) -> None:
        """Start background task for periodic session cleanup.
        
        Args:
            interval_minutes: Cleanup interval in minutes
            
        Implementation Notes:
            - Uses asyncio.create_task() for non-blocking execution
            - Includes error handling and retry logic
            - Logs scheduler status and metrics
            - Gracefully handles application shutdown
        """
        if self._cleanup_task and not self._cleanup_task.done():
            self.logger.warning("Cleanup scheduler already running")
            return
            
        self.logger.info(f"Starting session cleanup scheduler (interval: {interval_minutes} minutes)")
        
        async def cleanup_loop():
            """Background cleanup loop with error handling and graceful shutdown"""
            while not self._shutdown_event.is_set():
                try:
                    # Wait for either the interval or shutdown signal
                    try:
                        await asyncio.wait_for(
                            self._shutdown_event.wait(), 
                            timeout=interval_minutes * 60
                        )
                        # If we get here, shutdown was signaled
                        break
                    except asyncio.TimeoutError:
                        # Timeout is expected - time to run cleanup
                        pass
                    
                    if self._shutdown_event.is_set():
                        break
                        
                    # Run cleanup
                    self.logger.debug("Running scheduled session cleanup")
                    cleaned_count = await self.cleanup_inactive_sessions()
                    
                    if cleaned_count > 0:
                        self.logger.info(f"Scheduled cleanup removed {cleaned_count} sessions")
                    else:
                        self.logger.debug("Scheduled cleanup found no sessions to remove")
                        
                except SessionCleanupException as e:
                    self.logger.error(f"Scheduled cleanup failed: {e}")
                    # Continue running, don't stop scheduler for cleanup failures
                    await asyncio.sleep(60)  # Wait 1 minute before retrying
                    
                except Exception as e:
                    self.logger.error(f"Unexpected error in cleanup scheduler: {e}")
                    # Wait before retrying to avoid tight error loops
                    await asyncio.sleep(60)
            
            self.logger.info("Session cleanup scheduler stopped")
        
        # Create the background task
        self._cleanup_task = asyncio.create_task(cleanup_loop())
        self.logger.info("Session cleanup scheduler started successfully")

    async def stop_cleanup_scheduler(self) -> None:
        """Stop the background cleanup scheduler gracefully."""
        if not self._cleanup_task or self._cleanup_task.done():
            self.logger.debug("Cleanup scheduler not running")
            return
            
        self.logger.info("Stopping session cleanup scheduler...")
        
        # Signal shutdown
        self._shutdown_event.set()
        
        try:
            # Wait for the task to complete with a reasonable timeout
            await asyncio.wait_for(self._cleanup_task, timeout=30.0)
            self.logger.info("Session cleanup scheduler stopped gracefully")
        except asyncio.TimeoutError:
            self.logger.warning("Cleanup scheduler did not stop gracefully, cancelling")
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        except Exception as e:
            self.logger.error(f"Error stopping cleanup scheduler: {e}")
        
        # Reset state
        self._cleanup_task = None
        self._shutdown_event.clear()

    def get_session_metrics(self) -> Dict[str, Union[int, float]]:
        """Get comprehensive session metrics for monitoring.
        
        Returns:
            Dictionary containing:
            - active_sessions: Current active session count
            - expired_sessions: Sessions awaiting cleanup
            - cleanup_runs: Total cleanup operations performed
            - last_cleanup_time: Timestamp of last cleanup
            - average_session_duration: Average session lifetime
            - memory_usage_mb: Estimated memory usage of session store
        """
        current_time = datetime.utcnow()
        
        # Count active vs expired sessions
        active_sessions = 0
        expired_sessions = 0
        cutoff_time = current_time - timedelta(minutes=self.session_timeout_minutes)
        
        for session_info in self._session_index.values():
            last_activity = session_info.get('last_activity', session_info.get('created_at'))
            if last_activity and last_activity > cutoff_time:
                active_sessions += 1
            else:
                expired_sessions += 1
        
        # Calculate average session duration
        average_duration = 0
        if self._session_durations:
            average_duration = sum(self._session_durations) / len(self._session_durations)
            # Keep only recent durations to avoid memory growth
            if len(self._session_durations) > 1000:
                self._session_durations = self._session_durations[-500:]
        
        # Estimate memory usage (rough calculation)
        # Each session index entry is roughly 200 bytes, plus session data estimate
        estimated_memory_mb = (len(self._session_index) * 0.0002) + (active_sessions * 0.001)
        
        return {
            "active_sessions": active_sessions,
            "expired_sessions": expired_sessions,
            "total_sessions": len(self._session_index),
            "cleanup_runs": self._cleanup_runs,
            "last_cleanup_time": self._last_cleanup_time.isoformat() if self._last_cleanup_time else None,
            "average_session_duration_hours": round(average_duration, 2),
            "memory_usage_mb": round(estimated_memory_mb, 2),
            "cleanup_scheduler_running": self._cleanup_task and not self._cleanup_task.done(),
            "session_timeout_minutes": self.session_timeout_minutes,
            "cleanup_interval_minutes": self.cleanup_interval_minutes
        }

    async def close(self):
        """
        Close the session store connection and stop cleanup scheduler
        """
        try:
            # Stop cleanup scheduler first
            await self.stop_cleanup_scheduler()
            
            # The ISessionStore interface doesn't define a close method
            # So we'll try to close the underlying Redis connection if it exists
            if hasattr(self.session_store, 'redis_client'):
                if hasattr(self.session_store.redis_client, 'aclose'):
                    await self.session_store.redis_client.aclose()
                elif hasattr(self.session_store.redis_client, 'close'):
                    await self.session_store.redis_client.close()
                self.logger.info("Session store connection closed")
            else:
                self.logger.debug("Session store doesn't support explicit close operation")
        except Exception as e:
            self.logger.warning(f"Error closing session store connection: {e}")
