"""Comprehensive Session Service Tests for Maximum Coverage

This module provides comprehensive test coverage for SessionService following 
FaultMaven testing patterns. Targets 75% coverage by testing all major 
code paths, error conditions, and business logic scenarios.

Coverage Areas:
- Session lifecycle management (create, update, cleanup)
- Session state coordination and transitions
- Session analytics and monitoring operations
- Cross-service session coordination
- Error handling and recovery scenarios
- Performance and concurrency validation
"""

import pytest
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import Mock, patch, AsyncMock, MagicMock

from faultmaven.services.domain.session_service import SessionService
from faultmaven.models import AgentState, SessionContext
# SessionManager has been replaced by SessionService architecture
# from faultmaven.session_management import SessionManager
from faultmaven.exceptions import ValidationException, ServiceException


class ComprehensiveMockSessionManager:
    """Comprehensive mock session manager for testing"""
    
    def __init__(self):
        self.sessions = {}
        self._test_attributes = {}  # Store additional test-specific attributes
        self.call_count = 0
        self.operations_log = []
        self.should_fail = False
        self.failure_type = None
        self.cleanup_count = 0
        
    async def create_session(self, user_id: str, initial_context: Optional[Dict[str, Any]] = None) -> SessionContext:
        """Mock session creation"""
        self.call_count += 1
        self.operations_log.append(("create_session", user_id, initial_context))
        
        if self.should_fail and self.failure_type == "create_error":
            raise Exception("Session creation failed")
        
        session_id = f"session_{len(self.sessions) + 1:04d}"
        
        session_context = SessionContext(
            session_id=session_id,
            user_id=user_id,
            created_at=datetime.utcnow(),
            last_activity=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            metadata=initial_context or {}
        )
        # Store additional test attributes in a separate dict
        self._test_attributes[session_id] = {
            'context': initial_context or {},
            'active': True,
            'updated_at': datetime.utcnow()
        }
        
        self.sessions[session_id] = session_context
        return session_context
    
    async def get_session(self, session_id: str) -> Optional[SessionContext]:
        """Mock session retrieval"""
        self.call_count += 1
        self.operations_log.append(("get_session", session_id))
        
        if self.should_fail and self.failure_type == "get_error":
            raise Exception("Session retrieval failed")
        
        return self.sessions.get(session_id)
    
    async def update_session(self, session_id: str, updates: Dict[str, Any]) -> bool:
        """Mock session update"""
        self.call_count += 1
        self.operations_log.append(("update_session", session_id, updates))
        
        if self.should_fail and self.failure_type == "update_error":
            raise Exception("Session update failed")
        
        if session_id in self.sessions:
            session = self.sessions[session_id]
            # Update test attributes
            if session_id in self._test_attributes:
                self._test_attributes[session_id]['context'].update(updates)
                self._test_attributes[session_id]['updated_at'] = datetime.utcnow()
            
            # Handle agent state updates
            if "agent_state" in updates:
                session.agent_state = updates["agent_state"]
            else:
                # If no explicit agent_state, update case_context with the updates
                if session.agent_state and isinstance(session.agent_state, dict):
                    session.agent_state["case_context"].update(updates)
                
            session.last_activity = datetime.utcnow()
            return True
        return False
    
    async def end_session(self, session_id: str) -> bool:
        """Mock session termination"""
        self.call_count += 1
        self.operations_log.append(("end_session", session_id))
        
        if self.should_fail and self.failure_type == "end_error":
            raise Exception("Session termination failed")
        
        if session_id in self.sessions:
            if session_id in self._test_attributes:
                self._test_attributes[session_id]['active'] = False
                self._test_attributes[session_id]['updated_at'] = datetime.utcnow()
            self.sessions[session_id].last_activity = datetime.utcnow()
            return True
        return False
    
    async def cleanup_expired_sessions(self, expiry_threshold: datetime) -> int:
        """Mock session cleanup"""
        self.cleanup_count += 1
        self.operations_log.append(("cleanup_expired_sessions", expiry_threshold))
        
        if self.should_fail and self.failure_type == "cleanup_error":
            raise Exception("Session cleanup failed")
        
        cleaned_count = 0
        for session_id, session in self.sessions.items():
            attrs = self._test_attributes.get(session_id, {})
            session_time = attrs.get('updated_at', session.last_activity)
            session_active = attrs.get('active', True)
            if session_time < expiry_threshold and session_active:
                if session_id in self._test_attributes:
                    self._test_attributes[session_id]['active'] = False
                cleaned_count += 1
        
        return cleaned_count
    
    async def list_sessions(self, user_id: Optional[str] = None) -> List[SessionContext]:
        """Mock session listing retrieval"""
        self.call_count += 1
        self.operations_log.append(("list_sessions", user_id))
        
        if self.should_fail and self.failure_type == "user_sessions_error":
            raise Exception("User sessions retrieval failed")
        
        if user_id:
            return [session for session_id, session in self.sessions.items() 
                    if session.user_id == user_id and self._test_attributes.get(session_id, {}).get('active', True)]
        else:
            return [session for session_id, session in self.sessions.items() if self._test_attributes.get(session_id, {}).get('active', True)]
    
    def get_session_count(self) -> int:
        """Get total session count"""
        return len([s for session_id, s in self.sessions.items() if self._test_attributes.get(session_id, {}).get('active', True)])
    
    def get_active_sessions(self) -> List[SessionContext]:
        """Get all active sessions"""
        return [s for session_id, s in self.sessions.items() if self._test_attributes.get(session_id, {}).get('active', True)]
    
    async def delete_session(self, session_id: str) -> bool:
        """Mock session deletion"""
        self.call_count += 1
        self.operations_log.append(("delete_session", session_id))
        
        if self.should_fail and self.failure_type == "delete_error":
            raise Exception("Session deletion failed")
        
        if session_id in self.sessions:
            del self.sessions[session_id]
            if session_id in self._test_attributes:
                del self._test_attributes[session_id]
            return True
        return False
    
    async def get_session_stats(self) -> Dict[str, Any]:
        """Mock session statistics"""
        return {
            "total_sessions": len(self.sessions),
            "active_sessions": len([s for session_id, s in self.sessions.items() if self._test_attributes.get(session_id, {}).get('active', True)]),
            "sessions_by_state": {
                "idle": len([s for s in self.sessions.values() if s.agent_state and s.agent_state.get("current_phase") == "initial"]),
                "processing": len([s for s in self.sessions.values() if s.agent_state and s.agent_state.get("current_phase") == "investigating"]),
                "completed": len([s for s in self.sessions.values() if s.agent_state and s.agent_state.get("current_phase") == "completed"]),
                "error": len([s for s in self.sessions.values() if s.agent_state and s.agent_state.get("current_phase") == "error"])
            }
        }
    
    def get_test_context(self, session_id: str) -> Dict[str, Any]:
        """Get test context for a session"""
        return self._test_attributes.get(session_id, {}).get('context', {})
    
    def is_active(self, session_id: str) -> bool:
        """Check if session is active in test context"""
        # If session doesn't exist at all, it's inactive
        if session_id not in self.sessions:
            return False
        return self._test_attributes.get(session_id, {}).get('active', True)
    


class TestSessionServiceComprehensive:
    """Comprehensive test suite for SessionService with maximum coverage"""
    
    @pytest.fixture
    def mock_session_manager(self):
        """Comprehensive session manager mock"""
        return ComprehensiveMockSessionManager()
    
    @pytest.fixture
    def session_service(self, mock_session_manager):
        """SessionService with mocked dependencies"""
        return SessionService(
            session_store=mock_session_manager,
            max_sessions_per_user=5,
            inactive_threshold_hours=24
        )
    
    @pytest.fixture
    def session_service_strict(self, mock_session_manager):
        """SessionService with strict limits for testing"""
        return SessionService(
            session_store=mock_session_manager,
            max_sessions_per_user=2,
            inactive_threshold_hours=1
        )
    
    # Test 1: Complete Session Creation Workflow
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_complete_session_creation_workflow(
        self, session_service, mock_session_manager
    ):
        """Test complete session creation with all components"""
        user_id = "test_user_001"
        initial_context = {
            "environment": "production",
            "user_role": "admin",
            "preferences": {"theme": "dark", "notifications": True}
        }
        
        # Create session
        session, was_resumed = await session_service.create_session(
            user_id=user_id,
            initial_context=initial_context
        )
        
        # Validate session creation
        assert isinstance(session, SessionContext)
        assert isinstance(was_resumed, bool)
        assert session.session_id.startswith("session_")
        assert mock_session_manager.call_count >= 1  # May involve multiple operations (create, get, update)
        session_id = session.session_id
        
        # Validate session exists in manager
        session = await session_service.get_session(session_id)
        assert session is not None
        assert session.user_id == user_id
        # Verify initial context is set in agent state
        assert session.agent_state["case_context"] == initial_context
        assert session.agent_state["current_phase"] == "initial"
        assert session.active is True  # Use the session's active property
        assert isinstance(session.created_at, datetime)
        assert isinstance(session.last_activity, datetime)
        assert isinstance(session.updated_at, datetime)
    
    # Test 2: Session Lifecycle Management
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_session_lifecycle_management(
        self, session_service, mock_session_manager
    ):
        """Test complete session lifecycle from creation to termination"""
        user_id = "lifecycle_user"
        
        # 1. Create session
        session, was_resumed = await session_service.create_session(user_id)
        assert session is not None
        session_id = session.session_id
        
        # 2. Verify session is active
        session = await session_service.get_session(session_id)
        assert session.active is True
        assert session.agent_state["current_phase"] == "initial"
        
        # 3. Update session state
        update_context = {"current_task": "troubleshooting", "priority": "high"}
        agent_state = {
            "session_id": session_id,
            "user_query": "troubleshooting",
            "current_phase": "investigating",
            "case_context": update_context,
            "findings": [],
            "recommendations": [],
            "confidence_score": 0.0,
            "tools_used": []
        }
        combined_updates = {**update_context, "agent_state": agent_state}
        success = await session_service.update_session(session_id, combined_updates)
        assert success is True
        
        # 4. Verify update
        updated_session = await session_service.get_session(session_id)
        assert updated_session is not None
        # The agent state should be updated to investigating phase
        assert updated_session.agent_state["current_phase"] == "investigating"
        
        # 5. End session
        end_success = await session_service.delete_session(session_id)
        assert end_success is True
        
        # 6. Verify session is deleted
        ended_session = await session_service.get_session(session_id)
        assert ended_session is None
    
    # Test 3: Session Validation Edge Cases
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_session_validation_edge_cases(self, session_service):
        """Test session validation for various edge cases"""
        # Test empty user ID (should succeed - anonymous session)
        empty_session, was_resumed = await session_service.create_session("")
        assert empty_session is not None
        assert empty_session.user_id == ""
        
        # Test None user ID (should succeed - anonymous session)
        none_session, was_resumed = await session_service.create_session(None)
        assert none_session is not None
        assert none_session.user_id is None
        
        # Test whitespace-only user ID (should succeed)
        whitespace_session, was_resumed = await session_service.create_session("   \\n\\t   ")
        assert whitespace_session is not None
        assert whitespace_session.user_id == "   \\n\\t   "
        
        # Test invalid session ID operations
        with pytest.raises(ValidationException, match="Session ID cannot be empty"):
            await session_service.get_session("")
        
        with pytest.raises(ValidationException, match="Session ID cannot be empty"):
            await session_service.update_session("", {})
        
        # Test delete with empty session ID (should return False)
        delete_result = await session_service.delete_session("")
        assert delete_result is False
        
        # Test very long user ID (should succeed)
        long_user_id = "user_" + "a" * 1000
        session, was_resumed = await session_service.create_session(long_user_id)
        assert isinstance(session, SessionContext)
    
    # Test 4: Maximum Sessions Per User Limit
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_maximum_sessions_per_user_limit(
        self, session_service_strict, mock_session_manager
    ):
        """Test enforcement of maximum sessions per user limit"""
        user_id = "limited_user"
        session_ids = []
        
        # Create sessions up to the limit
        for i in range(2):  # limit is 2 for strict service
            session, was_resumed = await session_service_strict.create_session(user_id)
            session_ids.append(session.session_id)
        
        # Verify both sessions were created
        assert len(session_ids) == 2
        user_sessions = await session_service_strict.get_user_sessions(user_id)
        assert len(user_sessions) == 2
        
        # Attempt to create one more session (should succeed by cleaning up oldest)
        third_session, was_resumed = await session_service_strict.create_session(user_id)
        assert third_session is not None

        # Delete one session and try again
        await session_service_strict.delete_session(session_ids[0])

        # Should now be able to create a new session
        new_session, was_resumed = await session_service_strict.create_session(user_id)
        assert isinstance(new_session, SessionContext)
    
    # Test 5: Session Context Management
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_session_context_management(
        self, session_service, mock_session_manager
    ):
        """Test comprehensive session context management"""
        user_id = "context_user"
        session, was_resumed = await session_service.create_session(user_id)
        session_id = session.session_id
        
        # Test incremental context updates
        context_updates = [
            {"step": "1", "action": "data_ingestion", "status": "started"},
            {"step": "2", "action": "classification", "data_type": "log_file"},
            {"step": "3", "action": "analysis", "findings_count": 5},
            {"step": "4", "action": "recommendations", "priority": "high"}
        ]
        
        for update in context_updates:
            # Create proper agent state structure
            agent_state_update = {
                "session_id": session_id,
                "user_query": "test query",
                "current_phase": "investigating",
                "case_context": update,
                "findings": [],
                "recommendations": [],
                "confidence_score": 0.0,
                "tools_used": []
            }
            success = await session_service.update_session(
                session_id, {"agent_state": agent_state_update, **update}, validate_state=False
            )
            assert success is True
        
        # Verify final session state
        final_session = await session_service.get_session(session_id)
        assert final_session is not None
        
        # The agent state should contain the accumulated updates
        assert final_session.agent_state is not None
        # Session should still be active after updates
        assert final_session.active is True
        
        # Test context overriding
        override_context = {"step": "5", "action": "completed", "findings_count": 10}
        final_agent_state = {
            "session_id": session_id,
            "user_query": "test query",
            "current_phase": "completed",
            "case_context": override_context,
            "findings": [],
            "recommendations": [],
            "confidence_score": 0.0,
            "tools_used": []
        }
        await session_service.update_session(
            session_id, {"agent_state": final_agent_state, **override_context}, validate_state=False
        )
        
        overridden_session = await session_service.get_session(session_id)
        assert overridden_session is not None
        # Verify the agent state was updated with the final phase
        assert overridden_session.agent_state["current_phase"] == "completed"
    
    # Test 6: Session State Transitions
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_session_state_transitions(
        self, session_service, mock_session_manager
    ):
        """Test agent state transitions throughout session lifecycle"""
        user_id = "state_user"
        session, was_resumed = await session_service.create_session(user_id)
        session_id = session.session_id
        
        # Test state transition sequence with proper agent state structure
        state_sequence = [
            ("investigating", {"task": "starting_analysis"}),
            ("analyzing", {"tool": "knowledge_search", "analysis_started": "now"}),
            ("investigating", {"tool_result": "found_relevant_docs", "next_step": "further_analysis"}),
            ("concluding", {"result": "troubleshooting_complete", "confidence": 0.85}),
            ("completed", {"final_result": "issue_resolved"})
        ]
        
        for phase, context in state_sequence:
            # Create proper agent state structure
            agent_state_update = {
                "session_id": session_id,
                "user_query": "test query",
                "current_phase": phase,
                "case_context": context,
                "findings": [],
                "recommendations": [],
                "confidence_score": 0.0,
                "tools_used": []
            }
            success = await session_service.update_session(
                session_id, {"agent_state": agent_state_update, **context}
            )
            assert success is True
            
            # Verify state was updated
            session = await session_service.get_session(session_id)
            assert session.agent_state["current_phase"] == phase
        
        # Test invalid state transitions (business logic validation)
        # Note: This depends on implementation - may not have restrictions
        final_session = await session_service.get_session(session_id)
        assert final_session.agent_state["current_phase"] == "completed"
    
    # Test 7: Session Analytics and Monitoring
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_session_analytics_and_monitoring(
        self, session_service, mock_session_manager
    ):
        """Test session analytics and monitoring functionality"""
        # Create multiple sessions for different users
        users_and_sessions = []
        for i in range(5):
            user_id = f"analytics_user_{i}"
            session, was_resumed = await session_service.create_session(
                user_id, {"user_type": "test", "created_for": "analytics"}
            )
            users_and_sessions.append((user_id, session.session_id))
        
        # Get analytics
        analytics = await session_service.get_session_analytics()
        
        # Validate analytics structure
        assert isinstance(analytics, dict)
        assert "total_sessions" in analytics
        assert "active_sessions" in analytics
        assert "sessions_by_state" in analytics
        assert "average_session_duration_hours" in analytics
        
        # Validate analytics data
        assert analytics["total_sessions"] >= 5
        assert analytics["active_sessions"] >= 5
        
        # Validate state distribution
        state_distribution = analytics["sessions_by_state"]
        assert isinstance(state_distribution, dict)
        assert "idle" in state_distribution
        
        # Test user-specific analytics
        user_analytics = await session_service.get_user_session_analytics(users_and_sessions[0][0])
        assert isinstance(user_analytics, dict)
        assert "user_id" in user_analytics
        assert "session_count" in user_analytics
        assert "total_duration" in user_analytics
        assert user_analytics["user_id"] == users_and_sessions[0][0]
    
    # Test 8: Session Cleanup Operations
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_session_cleanup_operations(
        self, session_service_strict, mock_session_manager
    ):
        """Test session cleanup and maintenance operations"""
        # Create sessions with different ages
        users = ["cleanup_user_1", "cleanup_user_2", "cleanup_user_3"]
        session_ids = []
        
        for user in users:
            session, was_resumed = await session_service_strict.create_session(user)
            session_ids.append(session.session_id)
        
        # Manually age some sessions by modifying the mock
        current_time = datetime.utcnow()
        old_time = current_time - timedelta(hours=2)  # Older than 1 hour threshold
        
        # Age the first two sessions by setting their updated_at time and last_activity
        for session_id in session_ids[:2]:
            if session_id in mock_session_manager._test_attributes:
                mock_session_manager._test_attributes[session_id]['updated_at'] = old_time
                # Also update the actual session object's last_activity
                if session_id in mock_session_manager.sessions:
                    mock_session_manager.sessions[session_id].last_activity = old_time
        
        # Run cleanup
        cleaned_count = await session_service_strict.cleanup_inactive_sessions()
        
        # Validate cleanup occurred
        assert cleaned_count == 2
        
        # Verify the right sessions were cleaned (should be inactive)
        for session_id in session_ids[:2]:
            assert not mock_session_manager.is_active(session_id)  # Cleaned sessions should be inactive
        
        # Verify the recent session is still active
        recent_session = await session_service_strict.get_session(session_ids[2])
        assert recent_session is not None
        assert recent_session.active is True
    
    # Test 9: Error Handling Scenarios
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_error_handling_scenarios(
        self, session_service, mock_session_manager
    ):
        """Test comprehensive error handling scenarios"""
        # Test session manager creation failure
        mock_session_manager.should_fail = True
        mock_session_manager.failure_type = "create_error"
        
        with pytest.raises(RuntimeError):
            await session_service.create_session("error_user")
        
        # Reset for next test
        mock_session_manager.should_fail = False
        
        # Create a session for update tests
        session, was_resumed = await session_service.create_session("update_user")
        session_id = session.session_id
        
        # Test session manager update failure
        mock_session_manager.should_fail = True
        mock_session_manager.failure_type = "update_error"
        
        with pytest.raises(RuntimeError):
            await session_service.update_session(session_id, {"test": "data"})
        
        # Reset and test get failure
        mock_session_manager.should_fail = False
        mock_session_manager.failure_type = "get_error"
        mock_session_manager.should_fail = True
        
        with pytest.raises(RuntimeError):
            await session_service.get_session(session_id)
        
        # Reset and test delete failure
        mock_session_manager.should_fail = False
        mock_session_manager.failure_type = "delete_error"
        mock_session_manager.should_fail = True
        
        delete_result = await session_service.delete_session(session_id)
        assert delete_result is False  # Should return False on failure
        
        # Reset and test cleanup failure (cleanup returns count, not raises)
        mock_session_manager.should_fail = False
        mock_session_manager.failure_type = "cleanup_error" 
        mock_session_manager.should_fail = True
        
        cleanup_count = await session_service.cleanup_inactive_sessions()
        assert cleanup_count == 0  # Should return 0 on failure
    
    # Test 11: Concurrent Session Operations
    @pytest.mark.asyncio
    @pytest.mark.unit
    @pytest.mark.performance
    async def test_concurrent_session_operations(
        self, session_service, mock_session_manager
    ):
        """Test concurrent session operations performance"""
        # Create multiple sessions concurrently
        user_ids = [f"concurrent_user_{i}" for i in range(10)]
        
        start_time = datetime.utcnow()
        session_results = await asyncio.gather(*[
            session_service.create_session(user_id) for user_id in user_ids
        ])
        end_time = datetime.utcnow()

        creation_time = (end_time - start_time).total_seconds()

        # Extract session IDs from SessionContext objects (unpack tuples)
        sessions = [result[0] for result in session_results]  # Get SessionContext from tuples
        session_ids = [session.session_id for session in sessions]
        
        # Validate all sessions were created
        assert len(session_ids) == 10
        for session_id in session_ids:
            assert isinstance(session_id, str)
            assert session_id.startswith("session_")
        
        # Validate performance
        assert creation_time < 1.0, f"Concurrent creation took {creation_time}s"
        
        # Test concurrent updates
        update_context = {"concurrent_test": True, "timestamp": datetime.utcnow().isoformat()}
        
        start_time = datetime.utcnow()
        update_results = await asyncio.gather(*[
            session_service.update_session(session_id, update_context)
            for session_id in session_ids
        ])
        end_time = datetime.utcnow()
        
        update_time = (end_time - start_time).total_seconds()
        
        # Validate all updates succeeded
        assert all(result is True for result in update_results)
        assert update_time < 1.0, f"Concurrent updates took {update_time}s"
    
    # Test 12: Session Context Size and Limits
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_session_context_size_and_limits(
        self, session_service, mock_session_manager
    ):
        """Test session context with large data and limits"""
        user_id = "large_context_user"
        session, was_resumed = await session_service.create_session(user_id)
        session_id = session.session_id
        
        # Test with large context data
        large_context = {
            "large_data": "x" * 10000,  # 10KB of data
            "complex_structure": {
                "nested_data": ["item_" + str(i) for i in range(100)],
                "metadata": {
                    "created_by": "test",
                    "tags": ["tag_" + str(i) for i in range(50)],
                    "settings": {f"setting_{i}": f"value_{i}" for i in range(20)}
                }
            },
            "array_data": [{"id": i, "data": "content_" + str(i)} for i in range(100)]
        }
        
        success = await session_service.update_session(session_id, large_context)
        assert success is True
        
        # Verify large context was stored
        session = await session_service.get_session(session_id)
        assert session is not None
        # The session should still be functional after large update
        assert session.active is True
    
    # Test 13: Session Search and Filtering
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_session_search_and_filtering(
        self, session_service, mock_session_manager
    ):
        """Test session search and filtering functionality"""
        # Create sessions with different characteristics
        session_configs = [
            ("search_user_1", {"environment": "production", "priority": "high"}),
            ("search_user_2", {"environment": "staging", "priority": "medium"}),
            ("search_user_3", {"environment": "production", "priority": "low"}),
            ("search_user_4", {"environment": "development", "priority": "high"})
        ]
        
        created_sessions = []
        for user_id, context in session_configs:
            session, was_resumed = await session_service.create_session(user_id, context)
            created_sessions.append((session.session_id, user_id, context))
        
        # Test filtering by environment
        prod_sessions = await session_service.get_sessions_by_criteria(
            {"environment": "production"}
        )
        assert len(prod_sessions) == 2
        
        # Test filtering by priority
        high_priority_sessions = await session_service.get_sessions_by_criteria(
            {"priority": "high"}
        )
        assert len(high_priority_sessions) == 2
        
        # Test multiple criteria filtering
        prod_high_sessions = await session_service.get_sessions_by_criteria(
            {"environment": "production", "priority": "high"}
        )
        assert len(prod_high_sessions) == 1
    
    # Test 14: Session Health Monitoring
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_session_health_monitoring(
        self, session_service, mock_session_manager
    ):
        """Test session health monitoring functionality"""
        # Create sessions in various states
        user_sessions = []
        for i in range(5):
            user_id = f"health_user_{i}"
            session, was_resumed = await session_service.create_session(user_id)
            session_id = session.session_id
            
            # Set different agent states - disable state validation for testing health monitoring
            phase_names = ["initial", "investigating", "waiting_for_input", "error", "completed"]
            agent_state_update = {
                "session_id": session_id,
                "user_query": f"test query {i}",
                "current_phase": phase_names[i],
                "case_context": {"health_test": True},
                "findings": [],
                "recommendations": [],
                "confidence_score": 0.0,
                "tools_used": []
            }
            await session_service.update_session(
                session_id, {"health_test": True, "agent_state": agent_state_update}, validate_state=False
            )
            user_sessions.append((user_id, session_id))
        
        # Get health status
        health_status = await session_service.get_session_health()
        
        
        # Validate health status structure
        assert isinstance(health_status, dict)
        assert "service_status" in health_status
        assert "total_sessions" in health_status
        assert "session_distribution" in health_status
        assert "error_sessions" in health_status
        assert "average_session_age" in health_status
        
        # Validate health metrics
        assert health_status["total_sessions"] >= 5
        assert health_status["service_status"] in ["healthy", "degraded", "unhealthy"]
        
        # Validate state distribution
        distribution = health_status["session_distribution"]
        assert "error" in distribution
        assert distribution["error"] == 1
    
    # Test 15: Comprehensive Business Logic Validation
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_comprehensive_business_logic_validation(
        self, session_service, mock_session_manager
    ):
        """Test comprehensive business logic validation"""
        # Test complex session workflow simulating real usage
        user_id = "business_logic_user"
        
        # 1. Create session with initial context
        initial_context = {
            "user_type": "enterprise",
            "subscription": "premium",
            "environment": "production",
            "region": "us-east-1"
        }
        session, was_resumed = await session_service.create_session(user_id, initial_context)
        session_id = session.session_id
        
        # 2. Simulate troubleshooting workflow steps with valid state transitions
        # Valid flow: initial -> investigating -> analyzing -> concluding -> completed
        workflow_steps = [
            ({"step": "1", "action": "problem_description", "issue_type": "database"}, "investigating"),
            ({"step": "2", "action": "data_ingestion", "data_type": "log_file", "size_mb": 5.2}, "investigating"),
            ({"step": "3", "action": "classification", "classification": "database_connectivity"}, "analyzing"),
            ({"step": "4", "action": "analysis", "patterns_found": 3, "confidence": 0.85}, "concluding"),
            ({"step": "5", "action": "recommendations", "recommendation_count": 5}, "completed")
        ]
        
        for context_update, phase in workflow_steps:
            # Create proper agent state structure
            agent_state_update = {
                "session_id": session_id,
                "user_query": "Database connection timeout affecting user transactions",
                "current_phase": phase,
                "case_context": context_update,
                "findings": [],
                "recommendations": [],
                "confidence_score": 0.0,
                "tools_used": []
            }
            success = await session_service.update_session(
                session_id, {"agent_state": agent_state_update, **context_update}, validate_state=False
            )
            assert success is True
            
            # Validate intermediate state
            session = await session_service.get_session(session_id)
            assert session.agent_state["current_phase"] == phase
        
        # 3. Validate final session state
        final_session = await session_service.get_session(session_id)
        assert final_session is not None

        # Validate final state and business logic consistency
        assert final_session.agent_state["current_phase"] == "completed"
        assert final_session.active is True
        assert final_session.user_id == user_id

        # 4. Test session analytics for business insights
        analytics = await session_service.get_session_analytics()
        assert analytics["total_sessions"] >= 1

        # 5. Clean up session
        end_success = await session_service.delete_session(session_id)
        assert end_success is True
        
        ended_session = await session_service.get_session(session_id)
        assert ended_session is None  # Session should be deleted