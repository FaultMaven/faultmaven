"""Test module for case service business logic.

This module tests the CaseService class which implements ICaseService interface,
focusing on business logic orchestration, access control, and service coordination.

Tests cover:
- Case lifecycle management (create, update, archive)
- Case-session association and linking
- Conversation context management
- Case sharing and collaboration
- Access control and permissions
- Error handling and edge cases
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch
from typing import List, Optional

from faultmaven.services.case import CaseService
from faultmaven.models.case import (
    Case,
    CaseMessage,
    CaseListFilter,
    CaseSearchRequest,
    CaseSummary,
    CaseStatus,
    CasePriority,
    MessageType,
    ParticipantRole
)
from faultmaven.models.interfaces_case import ICaseStore
from faultmaven.models.interfaces import ISessionStore
from faultmaven.exceptions import ValidationException, ServiceException


class MockCaseStore:
    """Mock implementation of ICaseStore for testing"""
    
    def __init__(self):
        self.cases = {}
        self.messages = {}
        self.create_case = AsyncMock(return_value=True)
        self.get_case = AsyncMock(return_value=None)
        self.update_case = AsyncMock(return_value=True)
        self.delete_case = AsyncMock(return_value=True)
        self.list_cases = AsyncMock(return_value=[])
        self.search_cases = AsyncMock(return_value=[])
        self.add_message_to_case = AsyncMock(return_value=True)
        self.get_case_messages = AsyncMock(return_value=[])
        self.get_user_cases = AsyncMock(return_value=[])
        self.add_case_participant = AsyncMock(return_value=True)
        self.remove_case_participant = AsyncMock(return_value=True)
        self.update_case_activity = AsyncMock(return_value=True)
        self.cleanup_expired_cases = AsyncMock(return_value=0)
        self.get_case_analytics = AsyncMock(return_value={})


class MockSessionStore:
    """Mock implementation of ISessionStore for testing"""
    
    def __init__(self):
        self.data = {}
        self.get = AsyncMock(return_value=None)
        self.set = AsyncMock(return_value=True)
        self.delete = AsyncMock(return_value=True)


@pytest.fixture
def mock_case_store():
    """Fixture providing mock case store"""
    return MockCaseStore()


@pytest.fixture
def mock_session_store():
    """Fixture providing mock session store"""
    return MockSessionStore()


@pytest.fixture
def case_service(mock_case_store, mock_session_store):
    """Fixture providing CaseService instance with mocked dependencies"""
    return CaseService(
        case_store=mock_case_store,
        session_store=mock_session_store,
        default_case_expiry_days=30,
        max_cases_per_user=100
    )


@pytest.fixture
def sample_case():
    """Fixture providing sample case for testing"""
    case = Case(
        case_id="case-123",
        title="Sample Test Case",
        description="Test case description",
        owner_id="user-456",
        status=CaseStatus.ACTIVE,
        priority=CasePriority.MEDIUM
    )
    case.add_participant("user-456", ParticipantRole.OWNER)
    return case


@pytest.fixture
def sample_message():
    """Fixture providing sample case message"""
    return CaseMessage(
        case_id="case-123",
        session_id="session-789",
        author_id="user-456",
        message_type=MessageType.USER_QUERY,
        content="Test message content"
    )


class TestCaseServiceInitialization:
    """Test CaseService initialization and configuration"""
    
    def test_case_service_init_minimal(self, mock_case_store):
        """Test CaseService initialization with minimal parameters"""
        service = CaseService(case_store=mock_case_store)
        
        assert service.case_store == mock_case_store
        assert service.session_store is None
        assert service.default_case_expiry_days == 30
        assert service.max_cases_per_user == 100
    
    def test_case_service_init_full(self, mock_case_store, mock_session_store):
        """Test CaseService initialization with all parameters"""
        service = CaseService(
            case_store=mock_case_store,
            session_store=mock_session_store,
            default_case_expiry_days=60,
            max_cases_per_user=200
        )
        
        assert service.case_store == mock_case_store
        assert service.session_store == mock_session_store
        assert service.default_case_expiry_days == 60
        assert service.max_cases_per_user == 200


class TestCaseCreation:
    """Test case creation functionality"""
    
    @pytest.mark.asyncio
    async def test_create_case_minimal(self, case_service, mock_case_store):
        """Test creating case with minimal parameters"""
        mock_case_store.create_case.return_value = True
        
        case = await case_service.create_case(title="Test Case")
        
        assert case.title == "Test Case"
        assert case.description is None
        assert case.owner_id is None
        assert case.status == CaseStatus.ACTIVE
        assert case.priority == CasePriority.MEDIUM
        mock_case_store.create_case.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_create_case_full_parameters(self, case_service, mock_case_store, mock_session_store):
        """Test creating case with all parameters"""
        mock_case_store.create_case.return_value = True
        
        case = await case_service.create_case(
            title="Full Test Case",
            description="Detailed description",
            owner_id="user-123",
            session_id="session-456",
            initial_message="Initial problem description"
        )
        
        assert case.title == "Full Test Case"
        assert case.description == "Detailed description"
        assert case.owner_id == "user-123"
        assert case.current_session_id == "session-456"
        assert "session-456" in case.session_ids
        assert len(case.participants) == 1
        assert case.participants[0].user_id == "user-123"
        assert case.participants[0].role == ParticipantRole.OWNER
        assert len(case.messages) == 1
        assert case.messages[0].content == "Initial problem description"
        assert case.messages[0].message_type == MessageType.USER_QUERY
        
        # Verify session store update was attempted
        mock_session_store.set.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_create_case_empty_title_error(self, case_service):
        """Test creating case with empty title raises ValidationException"""
        with pytest.raises(ValidationException, match="Case title cannot be empty"):
            await case_service.create_case(title="")
        
        with pytest.raises(ValidationException, match="Case title cannot be empty"):
            await case_service.create_case(title="   ")
    
    @pytest.mark.asyncio
    async def test_create_case_title_too_long_error(self, case_service):
        """Test creating case with title too long raises ValidationException"""
        long_title = "x" * 201
        
        with pytest.raises(ValidationException, match="Case title cannot exceed 200 characters"):
            await case_service.create_case(title=long_title)
    
    @pytest.mark.asyncio
    async def test_create_case_user_limit_exceeded(self, case_service, mock_case_store):
        """Test creating case when user has reached case limit"""
        # Mock existing active cases for user
        existing_cases = [
            CaseSummary(
                case_id=f"case-{i}",
                title=f"Case {i}",
                status=CaseStatus.ACTIVE,
                priority=CasePriority.MEDIUM,
                owner_id="user-123",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                last_activity_at=datetime.utcnow(),
                message_count=0,
                participant_count=1,
                tags=[]
            )
            for i in range(100)  # At the limit
        ]
        
        case_service.list_user_cases = AsyncMock(return_value=existing_cases)
        
        with pytest.raises(ValidationException, match="User has reached maximum case limit"):
            await case_service.create_case(
                title="Another Case",
                owner_id="user-123"
            )
    
    @pytest.mark.asyncio
    async def test_create_case_store_failure(self, case_service, mock_case_store):
        """Test creating case when store operation fails"""
        mock_case_store.create_case.return_value = False
        
        with pytest.raises(ServiceException, match="Failed to create case in store"):
            await case_service.create_case(title="Test Case")
    
    @pytest.mark.asyncio
    async def test_create_case_session_store_warning(self, case_service, mock_case_store, mock_session_store):
        """Test creating case with session store failure logs warning"""
        mock_case_store.create_case.return_value = True
        mock_session_store.set.side_effect = Exception("Session store error")
        
        with patch.object(case_service, 'logger') as mock_logger:
            case = await case_service.create_case(
                title="Test Case",
                session_id="session-123"
            )
            
            assert case is not None
            mock_logger.warning.assert_called_once()


class TestCaseRetrieval:
    """Test case retrieval functionality"""
    
    @pytest.mark.asyncio
    async def test_get_case_success(self, case_service, mock_case_store, sample_case):
        """Test successful case retrieval"""
        mock_case_store.get_case.return_value = sample_case
        
        result = await case_service.get_case("case-123")
        
        assert result == sample_case
        mock_case_store.get_case.assert_called_once_with("case-123")
    
    @pytest.mark.asyncio
    async def test_get_case_with_user_access_control(self, case_service, mock_case_store, sample_case):
        """Test case retrieval with user access control"""
        mock_case_store.get_case.return_value = sample_case
        mock_case_store.update_case.return_value = True
        
        # User has access
        result = await case_service.get_case("case-123", user_id="user-456")
        
        assert result == sample_case
        # Should update last accessed time
        mock_case_store.update_case.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_get_case_access_denied(self, case_service, mock_case_store, sample_case):
        """Test case retrieval with access denied"""
        mock_case_store.get_case.return_value = sample_case
        
        # User does not have access
        result = await case_service.get_case("case-123", user_id="user-999")
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_get_case_not_found(self, case_service, mock_case_store):
        """Test case retrieval when case doesn't exist"""
        mock_case_store.get_case.return_value = None
        
        result = await case_service.get_case("case-999")
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_get_case_empty_id(self, case_service):
        """Test case retrieval with empty case ID"""
        result = await case_service.get_case("")
        assert result is None
        
        result = await case_service.get_case("   ")
        assert result is None
    
    @pytest.mark.asyncio
    async def test_get_case_store_exception(self, case_service, mock_case_store):
        """Test case retrieval when store raises exception"""
        mock_case_store.get_case.side_effect = Exception("Store error")
        
        result = await case_service.get_case("case-123")
        
        assert result is None


class TestCaseUpdate:
    """Test case update functionality"""
    
    @pytest.mark.asyncio
    async def test_update_case_success(self, case_service, mock_case_store, sample_case):
        """Test successful case update"""
        mock_case_store.get_case.return_value = sample_case
        mock_case_store.update_case.return_value = True
        
        updates = {
            "title": "Updated Title",
            "description": "Updated description",
            "status": CaseStatus.INVESTIGATING.value,
            "priority": CasePriority.HIGH.value
        }
        
        result = await case_service.update_case("case-123", updates, user_id="user-456")
        
        assert result is True
        mock_case_store.update_case.assert_called_once()
        
        # Check that update metadata was added
        call_args = mock_case_store.update_case.call_args[0]
        updates_sent = call_args[1]
        assert "updated_at" in updates_sent
        assert "metadata" in updates_sent
        assert updates_sent["metadata"]["last_updated_by"] == "user-456"
    
    @pytest.mark.asyncio
    async def test_update_case_access_denied(self, case_service, mock_case_store, sample_case):
        """Test case update with access denied"""
        # Add a viewer who can't edit
        sample_case.add_participant("user-999", ParticipantRole.VIEWER)
        mock_case_store.get_case.return_value = sample_case
        
        updates = {"title": "New Title"}
        
        result = await case_service.update_case("case-123", updates, user_id="user-999")
        
        assert result is False
        mock_case_store.update_case.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_update_case_invalid_fields(self, case_service, mock_case_store, sample_case):
        """Test case update with invalid fields filtered out"""
        mock_case_store.get_case.return_value = sample_case
        mock_case_store.update_case.return_value = True
        
        updates = {
            "title": "Valid Title",  # Valid field
            "case_id": "hacker-id",  # Invalid field - should be filtered
            "participants": [],  # Invalid field - should be filtered
            "invalid_field": "value"  # Invalid field - should be filtered
        }
        
        result = await case_service.update_case("case-123", updates, user_id="user-456")
        
        assert result is True
        
        # Check that only valid fields were sent
        call_args = mock_case_store.update_case.call_args[0]
        updates_sent = call_args[1]
        assert "title" in updates_sent
        assert "case_id" not in updates_sent
        assert "participants" not in updates_sent
        assert "invalid_field" not in updates_sent
    
    @pytest.mark.asyncio
    async def test_update_case_empty_id_error(self, case_service):
        """Test case update with empty case ID"""
        with pytest.raises(ValidationException, match="Case ID cannot be empty"):
            await case_service.update_case("", {"title": "New Title"})
    
    @pytest.mark.asyncio
    async def test_update_case_empty_updates_error(self, case_service):
        """Test case update with empty updates"""
        with pytest.raises(ValidationException, match="Updates cannot be empty"):
            await case_service.update_case("case-123", {})
    
    @pytest.mark.asyncio
    async def test_update_case_no_valid_fields_error(self, case_service, mock_case_store, sample_case):
        """Test case update with no valid fields"""
        mock_case_store.get_case.return_value = sample_case
        
        updates = {"invalid_field": "value"}
        
        with pytest.raises(ValidationException, match="No valid update fields provided"):
            await case_service.update_case("case-123", updates, user_id="user-456")
    
    @pytest.mark.asyncio
    async def test_update_case_store_failure(self, case_service, mock_case_store, sample_case):
        """Test case update when store operation fails"""
        mock_case_store.get_case.return_value = sample_case
        mock_case_store.update_case.return_value = False
        
        updates = {"title": "New Title"}
        
        result = await case_service.update_case("case-123", updates, user_id="user-456")
        
        assert result is False


class TestCaseSharing:
    """Test case sharing functionality"""
    
    @pytest.mark.asyncio
    async def test_share_case_success(self, case_service, mock_case_store, sample_case):
        """Test successful case sharing"""
        mock_case_store.get_case.return_value = sample_case
        mock_case_store.add_case_participant.return_value = True
        mock_case_store.update_case.return_value = True
        
        result = await case_service.share_case(
            case_id="case-123",
            target_user_id="user-789",
            role=ParticipantRole.COLLABORATOR,
            sharer_user_id="user-456"
        )
        
        assert result is True
        mock_case_store.add_case_participant.assert_called_once_with(
            "case-123", "user-789", ParticipantRole.COLLABORATOR, "user-456"
        )
        mock_case_store.update_case.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_share_case_update_existing_participant(self, case_service, mock_case_store, sample_case):
        """Test sharing case with existing participant (role update)"""
        # Add existing participant with different role
        sample_case.add_participant("user-789", ParticipantRole.VIEWER)
        mock_case_store.get_case.return_value = sample_case
        mock_case_store.update_case.return_value = True
        
        result = await case_service.share_case(
            case_id="case-123",
            target_user_id="user-789",
            role=ParticipantRole.COLLABORATOR,
            sharer_user_id="user-456"
        )
        
        assert result is True
        mock_case_store.add_case_participant.assert_not_called()
        mock_case_store.update_case.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_share_case_same_role_no_change(self, case_service, mock_case_store, sample_case):
        """Test sharing case with participant who already has same role"""
        # Add existing participant with same role
        sample_case.add_participant("user-789", ParticipantRole.COLLABORATOR)
        mock_case_store.get_case.return_value = sample_case
        
        result = await case_service.share_case(
            case_id="case-123",
            target_user_id="user-789",
            role=ParticipantRole.COLLABORATOR,
            sharer_user_id="user-456"
        )
        
        assert result is True
        mock_case_store.add_case_participant.assert_not_called()
        mock_case_store.update_case.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_share_case_access_denied(self, case_service, mock_case_store, sample_case):
        """Test case sharing with access denied"""
        # Add a viewer who can't share
        sample_case.add_participant("user-999", ParticipantRole.VIEWER)
        mock_case_store.get_case.return_value = sample_case
        
        result = await case_service.share_case(
            case_id="case-123",
            target_user_id="user-789",
            role=ParticipantRole.COLLABORATOR,
            sharer_user_id="user-999"
        )
        
        assert result is False
        mock_case_store.add_case_participant.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_share_case_owner_role_error(self, case_service):
        """Test sharing case with owner role raises ValidationException"""
        with pytest.raises(ValidationException, match="Cannot assign owner role through sharing"):
            await case_service.share_case(
                case_id="case-123",
                target_user_id="user-789",
                role=ParticipantRole.OWNER,
                sharer_user_id="user-456"
            )
    
    @pytest.mark.asyncio
    async def test_share_case_empty_parameters_error(self, case_service):
        """Test case sharing with empty parameters"""
        with pytest.raises(ValidationException, match="Case ID and target user ID are required"):
            await case_service.share_case(
                case_id="",
                target_user_id="user-789",
                role=ParticipantRole.COLLABORATOR
            )
        
        with pytest.raises(ValidationException, match="Case ID and target user ID are required"):
            await case_service.share_case(
                case_id="case-123",
                target_user_id="",
                role=ParticipantRole.COLLABORATOR
            )


class TestCaseMessages:
    """Test case message functionality"""
    
    @pytest.mark.asyncio
    async def test_add_message_success(self, case_service, mock_case_store, sample_message):
        """Test successful message addition"""
        mock_case_store.add_message_to_case.return_value = True
        mock_case_store.update_case_activity.return_value = True
        
        result = await case_service.add_message_to_case(
            case_id="case-123",
            message=sample_message,
            session_id="session-789"
        )
        
        assert result is True
        mock_case_store.add_message_to_case.assert_called_once_with("case-123", sample_message)
        mock_case_store.update_case_activity.assert_called_once_with("case-123", "session-789")
        
        # Check that message was properly configured
        assert sample_message.case_id == "case-123"
        assert sample_message.session_id == "session-789"
    
    @pytest.mark.asyncio
    async def test_add_message_with_session_association(self, case_service, mock_case_store, sample_message):
        """Test message addition with new session association"""
        # Create a case without the session
        case = Case(case_id="case-123", title="Test Case")
        mock_case_store.add_message_to_case.return_value = True
        mock_case_store.update_case_activity.return_value = True
        mock_case_store.get_case.return_value = case
        mock_case_store.update_case.return_value = True
        
        result = await case_service.add_message_to_case(
            case_id="case-123",
            message=sample_message,
            session_id="session-new"
        )
        
        assert result is True
        # Should update case with new session
        mock_case_store.update_case.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_add_message_empty_parameters_error(self, case_service):
        """Test message addition with empty parameters"""
        with pytest.raises(ValidationException, match="Case ID and message are required"):
            await case_service.add_message_to_case(case_id="", message=None)
    
    @pytest.mark.asyncio
    async def test_add_message_store_failure(self, case_service, mock_case_store, sample_message):
        """Test message addition when store operation fails"""
        mock_case_store.add_message_to_case.return_value = False
        
        result = await case_service.add_message_to_case(
            case_id="case-123",
            message=sample_message
        )
        
        assert result is False


class TestSessionCaseIntegration:
    """Test session-case integration functionality"""
    
    @pytest.mark.asyncio
    async def test_get_or_create_case_existing(self, case_service, mock_session_store, mock_case_store):
        """Test getting existing case for session"""
        existing_case = Case(case_id="case-123", title="Existing Case")
        mock_session_store.get.return_value = "case-123"
        mock_case_store.get_case.return_value = existing_case
        
        case_id = await case_service.get_or_create_case_for_session(
            session_id="session-456",
            user_id="user-789"
        )
        
        assert case_id == "case-123"
        mock_session_store.get.assert_called_once_with("session:session-456:current_case_id")
        mock_case_store.get_case.assert_called_once_with("case-123", "user-789")
    
    @pytest.mark.asyncio
    async def test_get_or_create_case_expired(self, case_service, mock_session_store, mock_case_store):
        """Test creating new case when existing case is expired"""
        expired_case = Case(
            case_id="case-123",
            title="Expired Case",
            expires_at=datetime.utcnow() - timedelta(days=1)
        )
        
        mock_session_store.get.return_value = "case-123"
        mock_case_store.get_case.return_value = expired_case
        mock_case_store.create_case.return_value = True
        
        case_id = await case_service.get_or_create_case_for_session(
            session_id="session-456",
            user_id="user-789"
        )
        
        # Should create new case
        assert case_id != "case-123"
        mock_case_store.create_case.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_get_or_create_case_force_new(self, case_service, mock_case_store):
        """Test forcing creation of new case"""
        mock_case_store.create_case.return_value = True
        
        case_id = await case_service.get_or_create_case_for_session(
            session_id="session-456",
            user_id="user-789",
            force_new=True
        )
        
        assert case_id is not None
        mock_case_store.create_case.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_link_session_to_case_success(self, case_service, mock_case_store, mock_session_store):
        """Test successful session-case linking"""
        case = Case(case_id="case-123", title="Test Case")
        mock_case_store.get_case.return_value = case
        mock_case_store.update_case.return_value = True
        
        result = await case_service.link_session_to_case("session-456", "case-123")
        
        assert result is True
        mock_case_store.update_case.assert_called_once()
        mock_session_store.set.assert_called_once_with(
            "session:session-456:current_case_id",
            "case-123",
            ttl=86400
        )
    
    @pytest.mark.asyncio
    async def test_link_session_case_not_found(self, case_service, mock_case_store):
        """Test linking session to non-existent case"""
        mock_case_store.get_case.return_value = None
        
        result = await case_service.link_session_to_case("session-456", "case-999")
        
        assert result is False
    
    @pytest.mark.asyncio
    async def test_resume_case_in_session(self, case_service, mock_case_store):
        """Test resuming case in new session"""
        mock_case_store.get_case.return_value = Case(case_id="case-123", title="Test Case")
        mock_case_store.update_case.return_value = True
        mock_case_store.add_message_to_case.return_value = True
        mock_case_store.update_case_activity.return_value = True
        
        result = await case_service.resume_case_in_session("case-123", "session-new")
        
        assert result is True
        # Should add resume message
        mock_case_store.add_message_to_case.assert_called_once()


class TestConversationContext:
    """Test conversation context functionality"""
    
    @pytest.mark.asyncio
    async def test_get_conversation_context_success(self, case_service, mock_case_store):
        """Test successful conversation context generation"""
        messages = [
            CaseMessage(
                case_id="case-123",
                message_type=MessageType.USER_QUERY,
                content="What's wrong with the database?",
                timestamp=datetime.utcnow() - timedelta(minutes=10)
            ),
            CaseMessage(
                case_id="case-123",
                message_type=MessageType.AGENT_RESPONSE,
                content="Let me analyze the database logs for you.",
                timestamp=datetime.utcnow() - timedelta(minutes=9)
            ),
            CaseMessage(
                case_id="case-123",
                message_type=MessageType.SYSTEM_EVENT,
                content="Log analysis completed",
                timestamp=datetime.utcnow() - timedelta(minutes=8)
            )
        ]
        
        mock_case_store.get_case_messages.return_value = messages
        
        context = await case_service.get_case_conversation_context("case-123", limit=10)
        
        assert context != ""
        assert "Previous conversation in this troubleshooting case:" in context
        assert "What's wrong with the database?" in context
        assert "Let me analyze the database logs" in context
        assert "Log analysis completed" in context
        assert "Current query:" in context
    
    @pytest.mark.asyncio
    async def test_get_conversation_context_empty(self, case_service, mock_case_store):
        """Test conversation context with no messages"""
        mock_case_store.get_case_messages.return_value = []
        
        context = await case_service.get_case_conversation_context("case-123")
        
        assert context == ""
    
    @pytest.mark.asyncio
    async def test_get_conversation_context_long_response_truncation(self, case_service, mock_case_store):
        """Test conversation context with long agent response truncation"""
        long_response = "x" * 300  # Longer than 200 char limit
        
        messages = [
            CaseMessage(
                case_id="case-123",
                message_type=MessageType.AGENT_RESPONSE,
                content=long_response,
                timestamp=datetime.utcnow()
            )
        ]
        
        mock_case_store.get_case_messages.return_value = messages
        
        context = await case_service.get_case_conversation_context("case-123")
        
        assert "..." in context  # Should be truncated


class TestCaseArchiving:
    """Test case archiving functionality"""
    
    @pytest.mark.asyncio
    async def test_archive_case_success_owner(self, case_service, mock_case_store, sample_case):
        """Test successful case archiving by owner"""
        mock_case_store.get_case.return_value = sample_case
        mock_case_store.update_case.return_value = True
        
        result = await case_service.archive_case(
            case_id="case-123",
            reason="Issue resolved",
            user_id="user-456"
        )
        
        assert result is True
        mock_case_store.update_case.assert_called_once()
        
        # Check archive parameters
        call_args = mock_case_store.update_case.call_args[0]
        updates = call_args[1]
        assert updates["status"] == CaseStatus.ARCHIVED.value
        assert updates["metadata"]["archive_reason"] == "Issue resolved"
        assert updates["metadata"]["archived_by"] == "user-456"
    
    @pytest.mark.asyncio
    async def test_archive_case_success_collaborator(self, case_service, mock_case_store, sample_case):
        """Test successful case archiving by collaborator"""
        sample_case.add_participant("user-collab", ParticipantRole.COLLABORATOR)
        mock_case_store.get_case.return_value = sample_case
        mock_case_store.update_case.return_value = True
        
        result = await case_service.archive_case(
            case_id="case-123",
            user_id="user-collab"
        )
        
        assert result is True
    
    @pytest.mark.asyncio
    async def test_archive_case_access_denied(self, case_service, mock_case_store, sample_case):
        """Test case archiving with access denied"""
        sample_case.add_participant("user-viewer", ParticipantRole.VIEWER)
        mock_case_store.get_case.return_value = sample_case
        
        result = await case_service.archive_case(
            case_id="case-123",
            user_id="user-viewer"
        )
        
        assert result is False
        mock_case_store.update_case.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_archive_case_without_user_check(self, case_service, mock_case_store):
        """Test case archiving without user access check"""
        mock_case_store.update_case.return_value = True
        
        result = await case_service.archive_case(case_id="case-123")
        
        assert result is True
        mock_case_store.update_case.assert_called_once()


class TestCaseListingAndSearch:
    """Test case listing and search functionality"""
    
    @pytest.mark.asyncio
    async def test_list_user_cases_success(self, case_service, mock_case_store):
        """Test successful user case listing"""
        expected_cases = [
            CaseSummary(
                case_id="case-1",
                title="Case 1",
                status=CaseStatus.ACTIVE,
                priority=CasePriority.MEDIUM,
                owner_id="user-123",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                last_activity_at=datetime.utcnow(),
                message_count=5,
                participant_count=1,
                tags=["test"]
            )
        ]
        
        mock_case_store.get_user_cases.return_value = expected_cases
        
        result = await case_service.list_user_cases("user-123")
        
        assert result == expected_cases
        mock_case_store.get_user_cases.assert_called_once_with("user-123", None)
    
    @pytest.mark.asyncio
    async def test_list_user_cases_with_filters(self, case_service, mock_case_store):
        """Test user case listing with filters"""
        filters = CaseListFilter(status=CaseStatus.ACTIVE, limit=10)
        
        await case_service.list_user_cases("user-123", filters)
        
        mock_case_store.get_user_cases.assert_called_once_with("user-123", filters)
    
    @pytest.mark.asyncio
    async def test_list_user_cases_empty_user_error(self, case_service):
        """Test user case listing with empty user ID"""
        with pytest.raises(ValidationException, match="User ID cannot be empty"):
            await case_service.list_user_cases("")
    
    @pytest.mark.asyncio
    async def test_search_cases_success(self, case_service, mock_case_store):
        """Test successful case search"""
        search_request = CaseSearchRequest(query="database error")
        expected_results = []
        
        mock_case_store.search_cases.return_value = expected_results
        
        result = await case_service.search_cases(search_request, user_id="user-123")
        
        assert result == expected_results
        # Should add user filter
        assert search_request.filters.user_id == "user-123"
    
    @pytest.mark.asyncio
    async def test_search_cases_with_existing_filters(self, case_service, mock_case_store):
        """Test case search with existing filters"""
        filters = CaseListFilter(status=CaseStatus.ACTIVE)
        search_request = CaseSearchRequest(query="error", filters=filters)
        
        await case_service.search_cases(search_request, user_id="user-123")
        
        # Should add user ID to existing filters
        assert search_request.filters.user_id == "user-123"
        assert search_request.filters.status == CaseStatus.ACTIVE


class TestAnalyticsAndCleanup:
    """Test analytics and cleanup functionality"""
    
    @pytest.mark.asyncio
    async def test_get_case_analytics_success(self, case_service, mock_case_store):
        """Test successful case analytics retrieval"""
        expected_analytics = {
            "case_id": "case-123",
            "duration_hours": 24.5,
            "message_count": 10,
            "participant_count": 3
        }
        
        mock_case_store.get_case_analytics.return_value = expected_analytics
        
        result = await case_service.get_case_analytics("case-123")
        
        assert result == expected_analytics
        mock_case_store.get_case_analytics.assert_called_once_with("case-123")
    
    @pytest.mark.asyncio
    async def test_cleanup_expired_cases_success(self, case_service, mock_case_store):
        """Test successful expired case cleanup"""
        mock_case_store.cleanup_expired_cases.return_value = 5
        
        result = await case_service.cleanup_expired_cases()
        
        assert result == 5
        mock_case_store.cleanup_expired_cases.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_get_case_health_status(self, case_service):
        """Test case service health status"""
        health = await case_service.get_case_health_status()
        
        assert health["service_status"] == "healthy"
        assert health["case_store_connected"] is True
        assert health["session_store_connected"] is True
        assert health["default_expiry_days"] == 30
        assert health["max_cases_per_user"] == 100


class TestErrorHandling:
    """Test error handling and edge cases"""
    
    @pytest.mark.asyncio
    async def test_service_exception_propagation(self, case_service, mock_case_store):
        """Test that unexpected exceptions are wrapped in ServiceException"""
        mock_case_store.create_case.side_effect = Exception("Unexpected error")
        
        with pytest.raises(ServiceException, match="Case creation failed"):
            await case_service.create_case(title="Test Case")
    
    @pytest.mark.asyncio
    async def test_validation_exception_passthrough(self, case_service):
        """Test that ValidationExceptions are not wrapped"""
        with pytest.raises(ValidationException):
            await case_service.create_case(title="")
    
    @pytest.mark.asyncio
    async def test_logging_on_operations(self, case_service, mock_case_store):
        """Test that service operations are properly logged"""
        mock_case_store.create_case.return_value = True
        
        with patch.object(case_service, 'logger') as mock_logger:
            await case_service.create_case(title="Test Case")
            
            mock_logger.info.assert_called()
    
    @pytest.mark.asyncio
    async def test_warning_logs_on_failures(self, case_service, mock_case_store):
        """Test that failures generate warning logs"""
        mock_case_store.get_case_messages.side_effect = Exception("Store error")
        
        with patch.object(case_service, 'logger') as mock_logger:
            result = await case_service.get_case_conversation_context("case-123")
            
            assert result == ""
            mock_logger.warning.assert_called()