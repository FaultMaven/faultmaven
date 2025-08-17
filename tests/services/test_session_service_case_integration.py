"""Test module for session service case integration.

This module tests the integration between session service and case persistence
functionality, focusing on how sessions interact with cases.

Tests cover:
- Session-case lifecycle integration
- Case creation from sessions
- Session resumption with cases
- Message recording from sessions to cases
- Cross-session case continuity
- Session-case context management
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch
from typing import Optional, Dict, Any

from faultmaven.services.session_service import SessionService
from faultmaven.services.case_service import CaseService
from faultmaven.models.case import (
    Case,
    CaseMessage,
    CaseStatus,
    CasePriority,
    MessageType,
    ParticipantRole
)
from faultmaven.models import SessionContext, AgentState
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.exceptions import ValidationException, ServiceException


class MockCaseService:
    """Mock implementation of ICaseService for testing"""
    
    def __init__(self):
        self.cases = {}
        self.create_case = AsyncMock()
        self.get_case = AsyncMock(return_value=None)
        self.update_case = AsyncMock(return_value=True)
        self.share_case = AsyncMock(return_value=True)
        self.add_message_to_case = AsyncMock(return_value=True)
        self.get_or_create_case_for_session = AsyncMock()
        self.link_session_to_case = AsyncMock(return_value=True)
        self.get_case_conversation_context = AsyncMock(return_value="")
        self.resume_case_in_session = AsyncMock(return_value=True)
        self.archive_case = AsyncMock(return_value=True)
        self.list_user_cases = AsyncMock(return_value=[])
        self.search_cases = AsyncMock(return_value=[])
        self.get_case_analytics = AsyncMock(return_value={})
        self.cleanup_expired_cases = AsyncMock(return_value=0)


class MockSessionManager:
    """Mock session manager for testing"""
    
    def __init__(self):
        self.sessions = {}
        self.create_session = AsyncMock()
        self.get_session = AsyncMock(return_value=None)
        self.update_session = AsyncMock(return_value=True)
        self.delete_session = AsyncMock(return_value=True)
        self.cleanup_expired_sessions = AsyncMock(return_value=0)


@pytest.fixture
def mock_case_service():
    """Fixture providing mock case service"""
    return MockCaseService()


@pytest.fixture
def mock_session_manager():
    """Fixture providing mock session manager"""
    return MockSessionManager()


@pytest.fixture
def session_service(mock_session_manager, mock_case_service):
    """Fixture providing SessionService with case integration"""
    service = SessionService(session_manager=mock_session_manager)
    service.case_service = mock_case_service
    return service


@pytest.fixture
def sample_session_context():
    """Fixture providing sample session context"""
    return SessionContext(
        session_id="session-123",
        user_id="user-456",
        created_at=datetime.utcnow(),
        last_activity=datetime.utcnow(),
        agent_state=AgentState.IDLE,
        conversation_history=[],
        uploaded_data=[],
        insights={}
    )


@pytest.fixture
def sample_case():
    """Fixture providing sample case"""
    case = Case(
        case_id="case-123",
        title="Integration Test Case",
        description="Test case for session integration",
        owner_id="user-456",
        status=CaseStatus.ACTIVE,
        priority=CasePriority.MEDIUM
    )
    case.add_participant("user-456", ParticipantRole.OWNER)
    return case


class TestSessionCaseIntegration:
    """Test session-case integration functionality"""
    
    @pytest.mark.asyncio
    async def test_create_session_with_case_creation(self, session_service, mock_case_service, mock_session_manager, sample_case):
        """Test creating session that automatically creates a case"""
        # Setup mocks
        session_context = SessionContext(
            session_id="session-123",
            user_id="user-456",
            created_at=datetime.utcnow(),
            last_activity=datetime.utcnow(),
            agent_state=AgentState.IDLE,
            conversation_history=[],
            uploaded_data=[],
            insights={}
        )
        
        mock_session_manager.create_session.return_value = session_context
        mock_case_service.create_case.return_value = sample_case
        
        # Test session creation with case integration
        with patch.object(session_service, '_create_case_for_session') as mock_create_case:
            mock_create_case.return_value = sample_case
            
            result = await session_service.create_session(
                user_id="user-456",
                create_case=True,
                case_title="Auto-created Case"
            )
            
            assert result.session_id == "session-123"
            mock_create_case.assert_called_once_with(
                session_id="session-123",
                user_id="user-456",
                case_title="Auto-created Case"
            )
    
    @pytest.mark.asyncio
    async def test_resume_session_with_existing_case(self, session_service, mock_case_service, mock_session_manager, sample_case):
        """Test resuming session that has an associated case"""
        # Setup session with case reference
        session_context = SessionContext(
            session_id="session-123",
            user_id="user-456",
            created_at=datetime.utcnow(),
            last_activity=datetime.utcnow(),
            agent_state=AgentState.IDLE,
            conversation_history=[],
            uploaded_data=[],
            insights={"current_case_id": "case-123"}
        )
        
        mock_session_manager.get_session.return_value = session_context
        mock_case_service.get_case.return_value = sample_case
        mock_case_service.resume_case_in_session.return_value = True
        
        # Test session resumption
        result = await session_service.get_session("session-123")
        
        assert result.session_id == "session-123"
        assert result.insights.get("current_case_id") == "case-123"
    
    @pytest.mark.asyncio
    async def test_link_session_to_existing_case(self, session_service, mock_case_service, mock_session_manager, sample_session_context, sample_case):
        """Test linking an existing session to an existing case"""
        mock_session_manager.get_session.return_value = sample_session_context
        mock_case_service.get_case.return_value = sample_case
        mock_case_service.link_session_to_case.return_value = True
        mock_session_manager.update_session.return_value = True
        
        # Add method to service
        async def link_session_to_case(session_id: str, case_id: str) -> bool:
            """Link session to case"""
            session = await session_service.session_manager.get_session(session_id)
            if not session:
                return False
            
            case = await session_service.case_service.get_case(case_id, session.user_id)
            if not case:
                return False
            
            # Link in case service
            link_success = await session_service.case_service.link_session_to_case(session_id, case_id)
            if not link_success:
                return False
            
            # Update session with case reference
            session.insights["current_case_id"] = case_id
            await session_service.session_manager.update_session(session_id, session)
            
            return True
        
        session_service.link_session_to_case = link_session_to_case
        
        result = await session_service.link_session_to_case("session-123", "case-123")
        
        assert result is True
        mock_case_service.link_session_to_case.assert_called_once_with("session-123", "case-123")
        mock_session_manager.update_session.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_record_message_to_case(self, session_service, mock_case_service, mock_session_manager, sample_session_context):
        """Test recording session message to associated case"""
        # Setup session with case
        sample_session_context.insights["current_case_id"] = "case-123"
        mock_session_manager.get_session.return_value = sample_session_context
        mock_case_service.add_message_to_case.return_value = True
        
        # Add method to service
        async def record_message_to_case(
            session_id: str, 
            message_content: str, 
            message_type: MessageType = MessageType.USER_QUERY,
            author_id: Optional[str] = None
        ) -> bool:
            """Record a message from session to its associated case"""
            session = await session_service.session_manager.get_session(session_id)
            if not session:
                return False
            
            case_id = session.insights.get("current_case_id")
            if not case_id:
                return False
            
            message = CaseMessage(
                case_id=case_id,
                session_id=session_id,
                author_id=author_id or session.user_id,
                message_type=message_type,
                content=message_content
            )
            
            return await session_service.case_service.add_message_to_case(
                case_id, message, session_id
            )
        
        session_service.record_message_to_case = record_message_to_case
        
        result = await session_service.record_message_to_case(
            session_id="session-123",
            message_content="Test message from session",
            message_type=MessageType.USER_QUERY
        )
        
        assert result is True
        mock_case_service.add_message_to_case.assert_called_once()
        
        # Verify message details
        call_args = mock_case_service.add_message_to_case.call_args
        case_id, message, session_id = call_args[0]
        assert case_id == "case-123"
        assert message.content == "Test message from session"
        assert message.message_type == MessageType.USER_QUERY
        assert message.session_id == "session-123"
    
    @pytest.mark.asyncio
    async def test_get_case_context_for_session(self, session_service, mock_case_service, mock_session_manager, sample_session_context):
        """Test getting case conversation context for session"""
        # Setup session with case
        sample_session_context.insights["current_case_id"] = "case-123"
        mock_session_manager.get_session.return_value = sample_session_context
        mock_case_service.get_case_conversation_context.return_value = "Previous conversation context"
        
        # Add method to service
        async def get_case_context_for_session(session_id: str) -> str:
            """Get case conversation context for session"""
            session = await session_service.session_manager.get_session(session_id)
            if not session:
                return ""
            
            case_id = session.insights.get("current_case_id")
            if not case_id:
                return ""
            
            return await session_service.case_service.get_case_conversation_context(case_id)
        
        session_service.get_case_context_for_session = get_case_context_for_session
        
        context = await session_service.get_case_context_for_session("session-123")
        
        assert context == "Previous conversation context"
        mock_case_service.get_case_conversation_context.assert_called_once_with("case-123")
    
    @pytest.mark.asyncio
    async def test_cross_session_case_continuity(self, session_service, mock_case_service, mock_session_manager, sample_case):
        """Test case continuity across multiple sessions"""
        # First session creates case
        session1 = SessionContext(
            session_id="session-1",
            user_id="user-456",
            created_at=datetime.utcnow() - timedelta(hours=2),
            last_activity=datetime.utcnow() - timedelta(hours=2),
            agent_state=AgentState.COMPLETED,
            conversation_history=[],
            uploaded_data=[],
            insights={"current_case_id": "case-123"}
        )
        
        # Second session resumes case
        session2 = SessionContext(
            session_id="session-2",
            user_id="user-456",
            created_at=datetime.utcnow(),
            last_activity=datetime.utcnow(),
            agent_state=AgentState.IDLE,
            conversation_history=[],
            uploaded_data=[],
            insights={}
        )
        
        mock_case_service.get_case.return_value = sample_case
        mock_case_service.resume_case_in_session.return_value = True
        
        # Add method to service
        async def resume_case_in_new_session(session_id: str, case_id: str) -> bool:
            """Resume existing case in new session"""
            session = await session_service.session_manager.get_session(session_id)
            if not session:
                return False
            
            case = await session_service.case_service.get_case(case_id, session.user_id)
            if not case:
                return False
            
            # Resume case in session
            resume_success = await session_service.case_service.resume_case_in_session(case_id, session_id)
            if not resume_success:
                return False
            
            # Update session with case reference
            session.insights["current_case_id"] = case_id
            await session_service.session_manager.update_session(session_id, session)
            
            return True
        
        session_service.resume_case_in_new_session = resume_case_in_new_session
        
        # Test resuming case in new session
        mock_session_manager.get_session.return_value = session2
        mock_session_manager.update_session.return_value = True
        
        result = await session_service.resume_case_in_new_session("session-2", "case-123")
        
        assert result is True
        mock_case_service.resume_case_in_session.assert_called_once_with("case-123", "session-2")
        mock_session_manager.update_session.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_session_case_analytics_integration(self, session_service, mock_case_service, mock_session_manager, sample_session_context):
        """Test analytics integration between sessions and cases"""
        sample_session_context.insights["current_case_id"] = "case-123"
        mock_session_manager.get_session.return_value = sample_session_context
        
        case_analytics = {
            "case_id": "case-123",
            "duration_hours": 5.5,
            "message_count": 15,
            "participant_count": 2,
            "status": "active"
        }
        mock_case_service.get_case_analytics.return_value = case_analytics
        
        # Add method to service
        async def get_session_case_analytics(session_id: str) -> Dict[str, Any]:
            """Get analytics for session's associated case"""
            session = await session_service.session_manager.get_session(session_id)
            if not session:
                return {}
            
            case_id = session.insights.get("current_case_id")
            if not case_id:
                return {}
            
            case_analytics = await session_service.case_service.get_case_analytics(case_id)
            
            return {
                "session_id": session_id,
                "case_analytics": case_analytics,
                "session_duration_hours": (
                    (session.last_activity - session.created_at).total_seconds() / 3600
                ),
                "session_case_linked": True
            }
        
        session_service.get_session_case_analytics = get_session_case_analytics
        
        analytics = await session_service.get_session_case_analytics("session-123")
        
        assert analytics["session_id"] == "session-123"
        assert analytics["case_analytics"]["case_id"] == "case-123"
        assert analytics["case_analytics"]["message_count"] == 15
        assert analytics["session_case_linked"] is True
        assert "session_duration_hours" in analytics
    
    @pytest.mark.asyncio
    async def test_session_without_case_integration(self, session_service, mock_case_service, mock_session_manager, sample_session_context):
        """Test session operations when no case is associated"""
        # Session without case reference
        mock_session_manager.get_session.return_value = sample_session_context
        
        # Add methods to service
        async def record_message_to_case(session_id: str, message_content: str, message_type: MessageType = MessageType.USER_QUERY) -> bool:
            session = await session_service.session_manager.get_session(session_id)
            if not session or not session.insights.get("current_case_id"):
                return False
            return True
        
        async def get_case_context_for_session(session_id: str) -> str:
            session = await session_service.session_manager.get_session(session_id)
            if not session or not session.insights.get("current_case_id"):
                return ""
            return "context"
        
        session_service.record_message_to_case = record_message_to_case
        session_service.get_case_context_for_session = get_case_context_for_session
        
        # Test operations that should gracefully handle no case
        message_recorded = await session_service.record_message_to_case("session-123", "Test message")
        case_context = await session_service.get_case_context_for_session("session-123")
        
        assert message_recorded is False
        assert case_context == ""


class TestSessionCaseLifecycle:
    """Test session-case lifecycle integration"""
    
    @pytest.mark.asyncio
    async def test_session_creation_with_case_auto_creation(self, session_service, mock_case_service, mock_session_manager, sample_case):
        """Test automatic case creation during session creation"""
        session_context = SessionContext(
            session_id="session-123",
            user_id="user-456",
            created_at=datetime.utcnow(),
            last_activity=datetime.utcnow(),
            agent_state=AgentState.IDLE,
            conversation_history=[],
            uploaded_data=[],
            insights={}
        )
        
        mock_session_manager.create_session.return_value = session_context
        mock_case_service.get_or_create_case_for_session.return_value = "case-123"
        mock_session_manager.update_session.return_value = True
        
        # Add enhanced session creation method
        async def create_session_with_case(
            user_id: str,
            auto_create_case: bool = True,
            case_title: Optional[str] = None
        ) -> SessionContext:
            """Create session with optional case creation"""
            session = await session_service.session_manager.create_session(user_id)
            
            if auto_create_case:
                case_id = await session_service.case_service.get_or_create_case_for_session(
                    session.session_id, user_id
                )
                session.insights["current_case_id"] = case_id
                await session_service.session_manager.update_session(session.session_id, session)
            
            return session
        
        session_service.create_session_with_case = create_session_with_case
        
        result = await session_service.create_session_with_case("user-456", auto_create_case=True)
        
        assert result.session_id == "session-123"
        assert result.insights.get("current_case_id") == "case-123"
        mock_case_service.get_or_create_case_for_session.assert_called_once_with("session-123", "user-456")
    
    @pytest.mark.asyncio
    async def test_session_cleanup_with_case_archiving(self, session_service, mock_case_service, mock_session_manager):
        """Test session cleanup that optionally archives associated case"""
        session_context = SessionContext(
            session_id="session-123",
            user_id="user-456",
            created_at=datetime.utcnow() - timedelta(days=2),
            last_activity=datetime.utcnow() - timedelta(days=1),
            agent_state=AgentState.COMPLETED,
            conversation_history=[],
            uploaded_data=[],
            insights={"current_case_id": "case-123"}
        )
        
        mock_session_manager.get_session.return_value = session_context
        mock_session_manager.delete_session.return_value = True
        mock_case_service.archive_case.return_value = True
        
        # Add enhanced cleanup method
        async def cleanup_session_with_case(
            session_id: str,
            archive_case: bool = False,
            archive_reason: Optional[str] = None
        ) -> bool:
            """Clean up session and optionally archive case"""
            session = await session_service.session_manager.get_session(session_id)
            if not session:
                return False
            
            # Archive case if requested
            if archive_case and session.insights.get("current_case_id"):
                case_id = session.insights["current_case_id"]
                await session_service.case_service.archive_case(
                    case_id, archive_reason, session.user_id
                )
            
            # Delete session
            return await session_service.session_manager.delete_session(session_id)
        
        session_service.cleanup_session_with_case = cleanup_session_with_case
        
        result = await session_service.cleanup_session_with_case(
            "session-123",
            archive_case=True,
            archive_reason="Session completed"
        )
        
        assert result is True
        mock_case_service.archive_case.assert_called_once_with(
            "case-123", "Session completed", "user-456"
        )
        mock_session_manager.delete_session.assert_called_once_with("session-123")
    
    @pytest.mark.asyncio
    async def test_session_state_sync_with_case(self, session_service, mock_case_service, mock_session_manager, sample_session_context, sample_case):
        """Test synchronizing session state with case status"""
        sample_session_context.insights["current_case_id"] = "case-123"
        sample_case.status = CaseStatus.SOLVED
        
        mock_session_manager.get_session.return_value = sample_session_context
        mock_case_service.get_case.return_value = sample_case
        mock_session_manager.update_session.return_value = True
        
        # Add state sync method
        async def sync_session_with_case_state(session_id: str) -> bool:
            """Sync session state with associated case status"""
            session = await session_service.session_manager.get_session(session_id)
            if not session:
                return False
            
            case_id = session.insights.get("current_case_id")
            if not case_id:
                return False
            
            case = await session_service.case_service.get_case(case_id, session.user_id)
            if not case:
                return False
            
            # Update session state based on case status
            if case.status == CaseStatus.SOLVED:
                session.agent_state = AgentState.COMPLETED
            elif case.status == CaseStatus.INVESTIGATING:
                session.agent_state = AgentState.PROCESSING
            elif case.status == CaseStatus.ARCHIVED:
                session.agent_state = AgentState.COMPLETED
            
            session.insights["case_status"] = case.status.value
            return await session_service.session_manager.update_session(session_id, session)
        
        session_service.sync_session_with_case_state = sync_session_with_case_state
        
        result = await session_service.sync_session_with_case_state("session-123")
        
        assert result is True
        mock_session_manager.update_session.assert_called_once()
        
        # Verify session state was updated
        call_args = mock_session_manager.update_session.call_args[0]
        updated_session = call_args[1]
        assert updated_session.agent_state == AgentState.COMPLETED
        assert updated_session.insights["case_status"] == "solved"


class TestErrorHandling:
    """Test error handling in session-case integration"""
    
    @pytest.mark.asyncio
    async def test_session_case_integration_with_missing_session(self, session_service, mock_case_service, mock_session_manager):
        """Test case operations when session doesn't exist"""
        mock_session_manager.get_session.return_value = None
        
        # Add method to test
        async def record_message_to_case(session_id: str, message_content: str) -> bool:
            session = await session_service.session_manager.get_session(session_id)
            return session is not None
        
        session_service.record_message_to_case = record_message_to_case
        
        result = await session_service.record_message_to_case("nonexistent-session", "Test message")
        
        assert result is False
    
    @pytest.mark.asyncio
    async def test_session_case_integration_with_missing_case(self, session_service, mock_case_service, mock_session_manager, sample_session_context):
        """Test session operations when associated case doesn't exist"""
        sample_session_context.insights["current_case_id"] = "nonexistent-case"
        mock_session_manager.get_session.return_value = sample_session_context
        mock_case_service.get_case.return_value = None
        
        # Add method to test
        async def get_case_for_session(session_id: str):
            session = await session_service.session_manager.get_session(session_id)
            if not session:
                return None
            
            case_id = session.insights.get("current_case_id")
            if not case_id:
                return None
            
            return await session_service.case_service.get_case(case_id, session.user_id)
        
        session_service.get_case_for_session = get_case_for_session
        
        result = await session_service.get_case_for_session("session-123")
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_session_case_service_failure(self, session_service, mock_case_service, mock_session_manager, sample_session_context):
        """Test handling of case service failures"""
        sample_session_context.insights["current_case_id"] = "case-123"
        mock_session_manager.get_session.return_value = sample_session_context
        mock_case_service.add_message_to_case.side_effect = Exception("Case service error")
        
        # Add error handling method
        async def record_message_with_error_handling(session_id: str, message_content: str) -> bool:
            try:
                session = await session_service.session_manager.get_session(session_id)
                if not session:
                    return False
                
                case_id = session.insights.get("current_case_id")
                if not case_id:
                    return False
                
                message = CaseMessage(
                    case_id=case_id,
                    session_id=session_id,
                    message_type=MessageType.USER_QUERY,
                    content=message_content
                )
                
                return await session_service.case_service.add_message_to_case(case_id, message, session_id)
            except Exception:
                return False
        
        session_service.record_message_with_error_handling = record_message_with_error_handling
        
        result = await session_service.record_message_with_error_handling("session-123", "Test message")
        
        assert result is False
    
    @pytest.mark.asyncio
    async def test_concurrent_session_case_access(self, session_service, mock_case_service, mock_session_manager, sample_session_context, sample_case):
        """Test concurrent access to session-case integration"""
        sample_session_context.insights["current_case_id"] = "case-123"
        mock_session_manager.get_session.return_value = sample_session_context
        mock_case_service.get_case.return_value = sample_case
        mock_case_service.add_message_to_case.return_value = True
        
        # Add concurrent operation method
        async def concurrent_message_recording(session_id: str, messages: list) -> list:
            """Record multiple messages concurrently"""
            async def record_single_message(message_content: str) -> bool:
                session = await session_service.session_manager.get_session(session_id)
                if not session:
                    return False
                
                case_id = session.insights.get("current_case_id")
                if not case_id:
                    return False
                
                message = CaseMessage(
                    case_id=case_id,
                    session_id=session_id,
                    message_type=MessageType.USER_QUERY,
                    content=message_content
                )
                
                return await session_service.case_service.add_message_to_case(case_id, message, session_id)
            
            import asyncio
            tasks = [record_single_message(msg) for msg in messages]
            return await asyncio.gather(*tasks, return_exceptions=True)
        
        session_service.concurrent_message_recording = concurrent_message_recording
        
        messages = ["Message 1", "Message 2", "Message 3"]
        results = await session_service.concurrent_message_recording("session-123", messages)
        
        assert len(results) == 3
        assert all(result is True for result in results)
        assert mock_case_service.add_message_to_case.call_count == 3


class TestPerformanceAndScaling:
    """Test performance aspects of session-case integration"""
    
    @pytest.mark.asyncio
    async def test_batch_session_case_operations(self, session_service, mock_case_service, mock_session_manager):
        """Test batch operations for session-case integration"""
        # Mock multiple sessions with cases
        sessions = [
            SessionContext(
                session_id=f"session-{i}",
                user_id=f"user-{i}",
                created_at=datetime.utcnow(),
                last_activity=datetime.utcnow(),
                agent_state=AgentState.IDLE,
                conversation_history=[],
                uploaded_data=[],
                insights={"current_case_id": f"case-{i}"}
            )
            for i in range(5)
        ]
        
        def mock_get_session(session_id):
            for session in sessions:
                if session.session_id == session_id:
                    return session
            return None
        
        mock_session_manager.get_session.side_effect = mock_get_session
        mock_case_service.get_case_analytics.return_value = {"message_count": 10}
        
        # Add batch analytics method
        async def get_batch_session_case_analytics(session_ids: list) -> Dict[str, Any]:
            """Get analytics for multiple sessions and their cases"""
            results = {}
            
            for session_id in session_ids:
                session = await session_service.session_manager.get_session(session_id)
                if session and session.insights.get("current_case_id"):
                    case_id = session.insights["current_case_id"]
                    analytics = await session_service.case_service.get_case_analytics(case_id)
                    results[session_id] = {
                        "case_id": case_id,
                        "analytics": analytics
                    }
            
            return results
        
        session_service.get_batch_session_case_analytics = get_batch_session_case_analytics
        
        session_ids = [f"session-{i}" for i in range(5)]
        results = await session_service.get_batch_session_case_analytics(session_ids)
        
        assert len(results) == 5
        for i in range(5):
            session_id = f"session-{i}"
            assert session_id in results
            assert results[session_id]["case_id"] == f"case-{i}"
            assert results[session_id]["analytics"]["message_count"] == 10
    
    @pytest.mark.asyncio
    async def test_session_case_memory_efficiency(self, session_service, mock_case_service, mock_session_manager, sample_session_context):
        """Test memory efficiency of session-case operations"""
        # Test that we don't load unnecessary data
        sample_session_context.insights["current_case_id"] = "case-123"
        mock_session_manager.get_session.return_value = sample_session_context
        
        # Mock case service to track what data is requested
        case_data_requests = []
        
        async def mock_get_case_conversation_context(case_id: str, limit: int = 10) -> str:
            case_data_requests.append(("context", case_id, limit))
            return "Limited context"
        
        mock_case_service.get_case_conversation_context.side_effect = mock_get_case_conversation_context
        
        # Add memory-efficient context method
        async def get_limited_case_context(session_id: str, limit: int = 5) -> str:
            """Get limited case context to save memory"""
            session = await session_service.session_manager.get_session(session_id)
            if not session:
                return ""
            
            case_id = session.insights.get("current_case_id")
            if not case_id:
                return ""
            
            return await session_service.case_service.get_case_conversation_context(case_id, limit)
        
        session_service.get_limited_case_context = get_limited_case_context
        
        context = await session_service.get_limited_case_context("session-123", limit=3)
        
        assert context == "Limited context"
        assert len(case_data_requests) == 1
        assert case_data_requests[0] == ("context", "case-123", 3)