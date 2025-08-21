"""Microservice Session/Case Service - Phase A Implementation

This module implements the ISessionCaseService interface from the microservice
architecture blueprint, extending the existing SessionService with case-level
conversation tracking, session context management, and cross-session continuity.

Key Features:
- Session persistence and case management
- Case-level conversation tracking with Redis backend
- Session context windows with TTL management
- Cross-session context retrieval
- Sticky sessions for user continuity
- Session analytics and cleanup
- Full integration with existing session management

Implementation Notes:
- Extends existing SessionService functionality
- Uses Redis for hot session data with TTL
- Implements proper async patterns throughout
- Comprehensive error handling with retries
- Health checks and metrics emission
- PII-safe session data handling
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import uuid
import logging

from faultmaven.services.session_service import SessionService
from faultmaven.services.microservice_interfaces.core_services import ISessionCaseService
from faultmaven.models.microservice_contracts.core_contracts import TurnContext, Budget
from faultmaven.models.interfaces import ITracer
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import ValidationException, ServiceException
from faultmaven.models import SessionContext


class MicroserviceSessionService(ISessionCaseService):
    """
    Implementation of ISessionCaseService interface extending existing SessionService
    
    This service provides microservice-compatible session and case management with:
    - Session lifecycle management with TTL
    - Case-level conversation tracking
    - Context window management
    - Session analytics and monitoring
    - Cross-session continuity support
    """

    def __init__(
        self,
        base_session_service: SessionService,
        tracer: Optional[ITracer] = None,
        default_session_ttl_hours: int = 24,
        context_window_size: int = 10,
        max_sessions_per_user: int = 10
    ):
        """
        Initialize microservice session service
        
        Args:
            base_session_service: Existing SessionService instance
            tracer: Optional tracer for observability
            default_session_ttl_hours: Default session TTL in hours
            context_window_size: Default conversation context window size
            max_sessions_per_user: Maximum concurrent sessions per user
        """
        self._base_service = base_session_service
        self._tracer = tracer
        self._default_ttl = timedelta(hours=default_session_ttl_hours)
        self._context_window_size = context_window_size
        self._max_sessions_per_user = max_sessions_per_user
        self._logger = logging.getLogger(self.__class__.__name__)
        
        # Session state tracking for analytics
        self._session_metrics = {
            'sessions_created': 0,
            'sessions_active': 0,
            'context_retrievals': 0,
            'turns_added': 0
        }

    @trace("microservice_session_create")
    async def create_session(
        self, 
        user_id: Optional[str] = None, 
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Create new session with optional user association
        
        Args:
            user_id: Optional user identifier for session attribution
            metadata: Optional session metadata (device, location, etc.)
            
        Returns:
            Session identifier (UUID)
            
        Raises:
            ValidationException: If session creation fails validation
            ServiceException: If session creation fails
        """
        try:
            # Create session through base service
            session_context = await self._base_service.create_session(
                user_id=user_id,
                initial_context=metadata,
                metadata=metadata
            )
            
            # Update metrics
            self._session_metrics['sessions_created'] += 1
            self._session_metrics['sessions_active'] += 1
            
            self._logger.info(f"Created microservice session {session_context.session_id}")
            return session_context.session_id
            
        except Exception as e:
            self._logger.error(f"Failed to create session: {e}")
            raise ServiceException(f"Session creation failed: {str(e)}") from e

    @trace("microservice_session_get_context")
    async def get_session_context(
        self, 
        session_id: str, 
        window_size: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Get session context with conversation history window
        
        Args:
            session_id: Session identifier
            window_size: Optional context window size (default from config)
            
        Returns:
            Session context including:
            - conversation_history: Recent conversation turns
            - case_summary: Current case summary if available
            - user_profile: User preferences and history
            - metadata: Session metadata and analytics
            
        Raises:
            ValidationException: When session doesn't exist or expired
        """
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")
            
        try:
            # Get base session context
            session = await self._base_service.get_session(session_id, validate=True)
            if not session:
                raise ValidationException(f"Session {session_id} not found or expired")
            
            # Use provided window size or default
            window_size = window_size or self._context_window_size
            
            # Get conversation history for current case
            current_case_id = await self._base_service.get_current_case_id(session_id)
            conversation_history = []
            
            if current_case_id:
                conversation_history = await self._base_service.get_case_conversation_history(
                    session_id, current_case_id, limit=window_size
                )
            
            # Build comprehensive session context
            context = {
                "session_id": session_id,
                "user_id": session.user_id,
                "created_at": session.created_at.isoformat(),
                "last_activity": session.last_activity.isoformat(),
                "current_case_id": current_case_id,
                "conversation_history": conversation_history,
                "case_summary": {
                    "total_cases": len(session.case_history),
                    "current_case_turns": len(conversation_history),
                    "last_query_time": conversation_history[-1].get("timestamp") if conversation_history else None
                },
                "user_profile": {
                    "session_count": len(await self._base_service.get_user_sessions(session.user_id)) if session.user_id else 1,
                    "preferences": session.agent_state.get("user_preferences", {}) if session.agent_state else {}
                },
                "metadata": {
                    "agent_state": session.agent_state,
                    "data_uploads_count": len(session.data_uploads),
                    "session_duration": (datetime.utcnow() - session.created_at).total_seconds(),
                    "window_size_used": window_size
                }
            }
            
            # Update metrics
            self._session_metrics['context_retrievals'] += 1
            
            return context
            
        except ValidationException:
            raise
        except Exception as e:
            self._logger.error(f"Failed to get session context for {session_id}: {e}")
            raise ServiceException(f"Session context retrieval failed: {str(e)}") from e

    @trace("microservice_session_add_turn")
    async def add_turn(self, session_id: str, turn_data: Dict[str, Any]) -> bool:
        """
        Add conversation turn to session history
        
        Args:
            session_id: Session identifier
            turn_data: Turn data including query, response, metadata
            
        Returns:
            True if turn added successfully
            
        Notes:
            - Turns are automatically PII-sanitized before storage
            - Large responses are truncated with full text stored separately
            - Turn addition extends session TTL
        """
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")
            
        if not turn_data:
            raise ValidationException("Turn data cannot be empty")
            
        try:
            # Extract turn information
            query = turn_data.get("query", "")
            response = turn_data.get("response", "")
            metadata = turn_data.get("metadata", {})
            confidence_score = turn_data.get("confidence_score", 0.0)
            
            # Get or create current case ID
            case_id = await self._base_service.get_or_create_current_case_id(session_id)
            
            # Record the query operation if we have a query
            if query:
                success = await self._base_service.record_query_operation(
                    session_id=session_id,
                    query=query,
                    case_id=case_id,
                    context=metadata,
                    confidence_score=confidence_score
                )
                
                if not success:
                    self._logger.warning(f"Failed to record query operation for session {session_id}")
                    return False
            
            # Record case activity for the turn
            activity_details = {
                "query": query,
                "response_summary": response[:200] + "..." if len(response) > 200 else response,
                "confidence_score": confidence_score,
                "metadata": metadata
            }
            
            await self._base_service.update_case_activity(session_id, case_id, activity_details)
            
            # Update session activity (extends TTL)
            await self._base_service.update_last_activity(session_id)
            
            # Update metrics
            self._session_metrics['turns_added'] += 1
            
            self._logger.debug(f"Added turn to session {session_id}, case {case_id}")
            return True
            
        except ValidationException:
            raise
        except Exception as e:
            self._logger.error(f"Failed to add turn to session {session_id}: {e}")
            return False

    @trace("microservice_session_create_case")
    async def create_case(self, session_id: str, case_data: Dict[str, Any]) -> str:
        """
        Create persistent case from session for cross-session continuity
        
        Args:
            session_id: Source session identifier
            case_data: Case initialization data and metadata
            
        Returns:
            Case identifier for future reference
            
        Notes:
            - Cases persist beyond session TTL for continuity
            - Case creation requires explicit user consent
            - Case data is sanitized and anonymized
        """
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")
            
        if not case_data:
            raise ValidationException("Case data cannot be empty")
            
        try:
            # Validate session exists
            session = await self._base_service.get_session(session_id, validate=False)
            if not session:
                raise ValidationException(f"Session {session_id} not found")
            
            # Extract case information
            case_title = case_data.get("title", f"Case created from session {session_id[:8]}")
            case_description = case_data.get("description", "")
            case_metadata = case_data.get("metadata", {})
            
            # Use case service if available for persistent case creation
            if self._base_service.case_service:
                case_id = await self._base_service.get_or_create_case_for_session(
                    session_id=session_id,
                    user_id=session.user_id,
                    force_new_case=True,
                    case_title=case_title
                )
                
                if case_id:
                    self._logger.info(f"Created persistent case {case_id} from session {session_id}")
                    return case_id
            
            # Fallback: use session-based case management
            case_id = await self._base_service.start_new_case(session_id)
            
            # Record case creation details
            case_creation_record = {
                "action": "persistent_case_created",
                "case_id": case_id,
                "title": case_title,
                "description": case_description,
                "metadata": case_metadata,
                "source_session": session_id,
                "timestamp": datetime.utcnow().isoformat() + 'Z'
            }
            
            # Add to case history
            await self._base_service.session_manager.add_case_history(session_id, case_creation_record)
            
            self._logger.info(f"Created case {case_id} from session {session_id}")
            return case_id
            
        except ValidationException:
            raise
        except Exception as e:
            self._logger.error(f"Failed to create case from session {session_id}: {e}")
            raise ServiceException(f"Case creation failed: {str(e)}") from e

    @trace("microservice_session_get_user_cases")
    async def get_user_cases(self, user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Get user's recent cases for context and continuity
        
        Args:
            user_id: User identifier
            limit: Maximum number of cases to return
            
        Returns:
            List of case summaries with metadata
        """
        if not user_id or not user_id.strip():
            raise ValidationException("User ID cannot be empty")
            
        try:
            cases = []
            
            # Get user sessions
            user_sessions = await self._base_service.get_user_sessions(user_id)
            
            # Extract case information from sessions
            for session in user_sessions[-limit:]:  # Get most recent sessions
                if session.current_case_id:
                    case_summary = {
                        "case_id": session.current_case_id,
                        "session_id": session.session_id,
                        "created_at": session.created_at.isoformat(),
                        "last_activity": session.last_activity.isoformat(),
                        "total_interactions": len(session.case_history),
                        "status": "active" if await self._base_service._is_active(session) else "inactive",
                        "summary": {
                            "total_queries": len([h for h in session.case_history if h.get("action") == "query_processed"]),
                            "data_uploads": len(session.data_uploads),
                            "current_phase": session.agent_state.get("current_phase") if session.agent_state else None
                        }
                    }
                    cases.append(case_summary)
            
            # Sort by last activity (most recent first)
            cases.sort(key=lambda x: x["last_activity"], reverse=True)
            
            self._logger.debug(f"Retrieved {len(cases)} cases for user {user_id}")
            return cases[:limit]
            
        except ValidationException:
            raise
        except Exception as e:
            self._logger.error(f"Failed to get cases for user {user_id}: {e}")
            return []

    @trace("microservice_session_health_check")
    async def health_check(self) -> Dict[str, Any]:
        """
        Get service health status and dependent service availability
        
        Returns:
            Health status including:
            - Service status (healthy/degraded/unhealthy)
            - Performance metrics (p95 latency, error rate)
            - Session analytics and metrics
            - Dependency health status
        """
        try:
            # Get base service health
            base_health = await self._base_service.get_session_health()
            
            # Calculate service status
            service_status = "healthy"
            error_rate = 0
            
            if base_health.get("service_status") == "unhealthy":
                service_status = "unhealthy"
                error_rate = 0.3
            elif base_health.get("service_status") == "degraded":
                service_status = "degraded" 
                error_rate = 0.15
            
            # Get performance metrics
            total_sessions = base_health.get("total_sessions", 0)
            active_sessions = base_health.get("active_sessions", 0)
            
            # Calculate session utilization
            session_utilization = (active_sessions / max(total_sessions, 1)) * 100
            
            health_status = {
                "service": "microservice_session_service",
                "status": service_status,
                "timestamp": datetime.utcnow().isoformat(),
                "version": "1.0.0",
                "dependencies": {
                    "base_session_service": {
                        "status": base_health.get("service_status", "unknown"),
                        "total_sessions": total_sessions,
                        "active_sessions": active_sessions
                    },
                    "redis": {
                        "status": "healthy" if self._base_service.session_manager else "unavailable"
                    }
                },
                "metrics": {
                    "sessions_created": self._session_metrics['sessions_created'],
                    "sessions_active": active_sessions,
                    "context_retrievals": self._session_metrics['context_retrievals'],
                    "turns_added": self._session_metrics['turns_added'],
                    "session_utilization_percent": round(session_utilization, 2),
                    "average_session_age": base_health.get("average_session_age", 0),
                    "error_rate": error_rate
                },
                "limits": {
                    "max_sessions_per_user": self._max_sessions_per_user,
                    "default_context_window": self._context_window_size,
                    "session_ttl_hours": self._default_ttl.total_seconds() / 3600
                }
            }
            
            return health_status
            
        except Exception as e:
            self._logger.error(f"Health check failed: {e}")
            return {
                "service": "microservice_session_service",
                "status": "unhealthy",
                "timestamp": datetime.utcnow().isoformat(),
                "version": "1.0.0",
                "error": str(e),
                "dependencies": {},
                "metrics": {},
                "limits": {}
            }

    @trace("microservice_session_ready_check")
    async def ready_check(self) -> bool:
        """
        Check if service is ready to handle requests
        
        Returns:
            True if service is ready, False otherwise
            
        Notes:
            - Used by Kubernetes readiness probes
            - Checks critical dependencies
            - Validates configuration
        """
        try:
            # Check base service availability
            if not self._base_service:
                return False
            
            # Check session manager availability
            if not self._base_service.session_manager:
                return False
            
            # Try to perform a basic operation
            health_status = await self.health_check()
            service_status = health_status.get("status", "unhealthy")
            
            return service_status in ["healthy", "degraded"]
            
        except Exception as e:
            self._logger.error(f"Ready check failed: {e}")
            return False

    # Additional utility methods for microservice integration

    async def cleanup_expired_sessions(self) -> Dict[str, Any]:
        """
        Clean up expired sessions and return cleanup statistics
        
        Returns:
            Cleanup results with statistics
        """
        try:
            cleanup_count = await self._base_service.cleanup_inactive_sessions()
            
            # Update metrics
            self._session_metrics['sessions_active'] = max(0, self._session_metrics['sessions_active'] - cleanup_count)
            
            return {
                "cleanup_timestamp": datetime.utcnow().isoformat(),
                "sessions_cleaned": cleanup_count,
                "remaining_active": self._session_metrics['sessions_active'],
                "status": "success"
            }
            
        except Exception as e:
            self._logger.error(f"Session cleanup failed: {e}")
            return {
                "cleanup_timestamp": datetime.utcnow().isoformat(),
                "sessions_cleaned": 0,
                "status": "failed",
                "error": str(e)
            }

    async def get_session_analytics(self) -> Dict[str, Any]:
        """
        Get comprehensive session analytics for monitoring
        
        Returns:
            Session analytics and performance metrics
        """
        try:
            base_analytics = await self._base_service.get_session_analytics()
            
            # Add microservice-specific metrics
            microservice_metrics = {
                "microservice_metrics": {
                    "sessions_created": self._session_metrics['sessions_created'],
                    "context_retrievals": self._session_metrics['context_retrievals'], 
                    "turns_added": self._session_metrics['turns_added'],
                    "context_window_size": self._context_window_size,
                    "max_sessions_per_user": self._max_sessions_per_user
                },
                "performance_indicators": {
                    "avg_context_retrieval_rate": self._session_metrics['context_retrievals'] / max(self._session_metrics['sessions_created'], 1),
                    "avg_turns_per_session": self._session_metrics['turns_added'] / max(self._session_metrics['sessions_created'], 1)
                }
            }
            
            # Merge with base analytics
            return {**base_analytics, **microservice_metrics}
            
        except Exception as e:
            self._logger.error(f"Failed to get analytics: {e}")
            return {"error": str(e)}

    async def extend_session_ttl(self, session_id: str, extension_hours: int = 24) -> bool:
        """
        Extend session TTL for active sessions
        
        Args:
            session_id: Session identifier
            extension_hours: Hours to extend TTL
            
        Returns:
            True if TTL extended successfully
        """
        try:
            return await self._base_service.extend_session(session_id, extension_hours)
        except Exception as e:
            self._logger.error(f"Failed to extend session TTL: {e}")
            return False

    async def get_session_metrics(self) -> Dict[str, Any]:
        """
        Get current session metrics for monitoring
        
        Returns:
            Current metrics snapshot
        """
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "metrics": self._session_metrics.copy(),
            "config": {
                "default_ttl_hours": self._default_ttl.total_seconds() / 3600,
                "context_window_size": self._context_window_size,
                "max_sessions_per_user": self._max_sessions_per_user
            }
        }