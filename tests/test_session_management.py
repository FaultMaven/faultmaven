"""Test module for faultmaven.session_management.

This test module follows FaultMaven testing patterns and ensures proper mocking
of the ISessionStore interface while testing SessionManager business logic.
"""

import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from faultmaven.models import AgentState, SessionContext
from faultmaven.models.interfaces import ISessionStore
from faultmaven.session_management import SessionManager


@pytest.fixture
def sample_session_data():
    """Create sample session data as it would be stored (with ISO strings)."""
    now = datetime.utcnow()
    return {
        'session_id': 'test-session-123',
        'user_id': 'test-user',
        'created_at': now.isoformat(),
        'last_activity': now.isoformat(),
        'data_uploads': [],
        'investigation_history': [],
        'agent_state': {
            "session_id": "test-session-123",
            "user_query": "Test query",
            "current_phase": "initial",
            "investigation_context": {},
            "findings": [],
            "recommendations": [],
            "confidence_score": 0.5,
            "tools_used": [],
        }
    }


@pytest.fixture
def mock_session_store():
    """Create a mock session store for testing."""
    store_mock = AsyncMock(spec=ISessionStore)
    
    # Set up proper return values to avoid AsyncMock objects
    store_mock.set.return_value = None  # set() should return None
    store_mock.get.return_value = None  # will be overridden in tests
    store_mock.delete.return_value = True  # delete() should return bool
    store_mock.exists.return_value = True  # exists() should return bool
    
    return store_mock


@pytest.fixture
def session_manager(mock_session_store):
    """Create a SessionManager with mock ISessionStore."""
    return SessionManager(session_store=mock_session_store)


class TestSessionManager:
    """Test cases for SessionManager class."""

    def test_init_default_configuration(self):
        """Test SessionManager initialization with default configuration."""
        mock_session_store = AsyncMock(spec=ISessionStore)
        session_manager = SessionManager(session_store=mock_session_store)
        assert session_manager.session_timeout_hours == 24  # 24 hours default

    def test_init_custom_configuration(self):
        """Test SessionManager initialization with custom configuration."""
        mock_session_store = AsyncMock(spec=ISessionStore)
        session_manager = SessionManager(session_store=mock_session_store)
        # The timeout is a property of SessionManager, not a constructor parameter
        assert session_manager.session_timeout_hours == 24  # Default value

    @pytest.mark.asyncio
    async def test_create_session_success(self, session_manager, mock_session_store):
        """Test successful session creation."""
        mock_session_store.set.return_value = None  # set() returns None
        
        session = await session_manager.create_session("test-user")

        assert isinstance(session, SessionContext)
        assert session.user_id == "test-user"
        assert session.session_id is not None
        mock_session_store.set.assert_called_once()
        
        # Verify the stored data structure
        call_args = mock_session_store.set.call_args
        session_id = call_args[0][0]
        session_data = call_args[0][1]
        ttl = call_args[1]['ttl']
        
        assert session_id == session.session_id
        assert session_data['user_id'] == "test-user"
        assert ttl == 24 * 3600  # 24 hours in seconds

    @pytest.mark.asyncio
    async def test_create_session_with_existing_user(
        self, session_manager, mock_session_store
    ):
        """Test creating multiple sessions for the same user."""
        mock_session_store.set.return_value = None

        session1 = await session_manager.create_session("test-user")
        session2 = await session_manager.create_session("test-user")

        assert session1.session_id != session2.session_id
        assert session1.user_id == session2.user_id == "test-user"
        assert mock_session_store.set.call_count == 2

    @pytest.mark.asyncio
    async def test_get_session_existing(
        self, session_manager, mock_session_store, sample_session_data
    ):
        """Test retrieving an existing session."""
        # Mock session store returning session data (as dict, not JSON string)
        mock_session_store.get.return_value = sample_session_data
        mock_session_store.set.return_value = None  # for the last_activity update

        retrieved_session = await session_manager.get_session("test-session-123")

        assert retrieved_session is not None
        assert retrieved_session.session_id == "test-session-123"
        assert retrieved_session.user_id == "test-user"
        mock_session_store.get.assert_called_once_with("test-session-123")

    @pytest.mark.asyncio
    async def test_get_session_nonexistent(self, session_manager, mock_session_store):
        """Test retrieving a non-existent session."""
        mock_session_store.get.return_value = None

        session = await session_manager.get_session("non-existent-session")

        assert session is None

    @pytest.mark.asyncio
    async def test_update_session_success(
        self, session_manager, mock_session_store, sample_session_data
    ):
        """Test successful session update."""
        # Mock getting the session first - needs to return data twice
        mock_session_store.get.return_value = sample_session_data
        mock_session_store.set.return_value = None

        updates = {
            "agent_state": {
                "session_id": "test-session-123",
                "user_query": "Updated query",
                "current_phase": "investigating",
                "investigation_context": {"key": "value"},
                "findings": [],
                "recommendations": [],
                "confidence_score": 0.8,
                "tools_used": ["tool1"],
            }
        }

        success = await session_manager.update_session("test-session-123", updates)
        assert success is True

    @pytest.mark.asyncio
    async def test_update_session_nonexistent(
        self, session_manager, mock_session_store
    ):
        """Test updating a non-existent session."""
        mock_session_store.get.return_value = None

        updates = {
            "agent_state": {
                "session_id": "non-existent",
                "user_query": "Test",
                "current_phase": "investigating",
                "investigation_context": {},
                "findings": [],
                "recommendations": [],
                "confidence_score": 0.5,
                "tools_used": [],
            }
        }

        success = await session_manager.update_session("non-existent-session", updates)
        assert success is False

    @pytest.mark.asyncio
    async def test_delete_session_existing(
        self, session_manager, mock_session_store
    ):
        """Test deleting an existing session."""
        mock_session_store.delete.return_value = True

        success = await session_manager.delete_session("test-session-123")

        assert success is True
        mock_session_store.delete.assert_called_once_with("test-session-123")

    @pytest.mark.asyncio
    async def test_delete_session_nonexistent(
        self, session_manager, mock_session_store
    ):
        """Test deleting a non-existent session."""
        mock_session_store.delete.return_value = False

        success = await session_manager.delete_session("non-existent-session")
        assert success is False

    @pytest.mark.asyncio
    async def test_add_conversation_message(
        self, session_manager, mock_session_store, sample_session_data
    ):
        """Test adding conversation message to session."""
        # Mock getting the session first - needs to return data twice
        mock_session_store.get.return_value = sample_session_data
        mock_session_store.set.return_value = None

        investigation_data = {
            "type": "conversation",
            "message": "User asked about error logs",
        }

        success = await session_manager.add_investigation_history(
            "test-session-123", investigation_data
        )
        assert success is True

    @pytest.mark.asyncio
    async def test_add_uploaded_data(
        self, session_manager, mock_session_store, sample_session_data
    ):
        """Test adding uploaded data to session."""
        # Mock getting the session first - needs to return data twice
        mock_session_store.get.return_value = sample_session_data
        mock_session_store.set.return_value = None

        success = await session_manager.add_data_upload("test-session-123", "data-123")
        assert success is True

    @pytest.mark.asyncio
    async def test_is_session_active(self, session_manager, mock_session_store):
        """Test checking if a session is active."""
        # Test active session
        mock_session_store.exists.return_value = True
        assert await session_manager.is_session_active("test-session-123") is True

        # Test inactive session
        mock_session_store.exists.return_value = False
        assert await session_manager.is_session_active("inactive-session") is False

    @pytest.mark.asyncio
    async def test_extend_session(
        self, session_manager, mock_session_store, sample_session_data
    ):
        """Test extending session timeout."""
        # Mock getting the session first - needs to return data twice
        mock_session_store.get.return_value = sample_session_data
        mock_session_store.set.return_value = None

        success = await session_manager.extend_session("test-session-123")
        assert success is True

    @pytest.mark.asyncio
    async def test_session_lifecycle(self, session_manager, mock_session_store):
        """Test complete session lifecycle."""
        # Mock successful operations
        mock_session_store.set.return_value = None
        mock_session_store.delete.return_value = True

        # Create session
        session = await session_manager.create_session("test-user")
        assert session is not None

        # Mock getting the session for update
        session_data = {
            'session_id': session.session_id,
            'user_id': session.user_id,
            'created_at': session.created_at.isoformat(),
            'last_activity': session.last_activity.isoformat(),
            'data_uploads': [],
            'investigation_history': []
        }
        mock_session_store.get.return_value = session_data

        # Update session
        updates = {
            "agent_state": {
                "session_id": session.session_id,
                "user_query": "Test query",
                "current_phase": "investigating",
                "investigation_context": {},
                "findings": [],
                "recommendations": [],
                "confidence_score": 0.7,
                "tools_used": [],
            }
        }
        assert await session_manager.update_session(session.session_id, updates) is True

        # Delete session
        assert await session_manager.delete_session(session.session_id) is True


    @pytest.mark.asyncio
    async def test_update_last_activity(
        self, session_manager, mock_session_store, sample_session_data
    ):
        """Test updating last activity for a session."""
        # Mock getting the session first - needs to return data twice
        mock_session_store.get.return_value = sample_session_data
        mock_session_store.set.return_value = None

        success = await session_manager.update_last_activity("test-session-123")
        assert success is True
        # The session is updated in both get_session and update_last_activity
        assert mock_session_store.set.call_count >= 1

    @pytest.mark.asyncio
    async def test_update_last_activity_nonexistent(
        self, session_manager, mock_session_store
    ):
        """Test updating last activity for non-existent session."""
        mock_session_store.get.return_value = None

        success = await session_manager.update_last_activity("non-existent-session")
        assert success is False

    @pytest.mark.asyncio
    async def test_update_last_activity_exception(
        self, session_manager, mock_session_store
    ):
        """Test updating last activity when operation fails."""
        # Mock raising an exception
        mock_session_store.get.side_effect = Exception("Connection failed")

        success = await session_manager.update_last_activity("test-session-123")
        assert success is False

    @pytest.mark.asyncio
    async def test_delete_session_exception(
        self, session_manager, mock_session_store
    ):
        """Test deleting session when operation fails."""
        # Mock raising an exception
        mock_session_store.delete.side_effect = Exception("Connection failed")

        success = await session_manager.delete_session("test-session-123")
        assert success is False

    @pytest.mark.asyncio
    async def test_update_session_store_exception(
        self, session_manager, mock_session_store
    ):
        """Test updating session when store operation fails."""
        # Mock the second get call (in update_session) to fail
        call_count = 0
        def get_side_effect(*args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call from get_session in update_session - return valid data
                from datetime import datetime
                now = datetime.utcnow()
                return {
                    'session_id': 'test-session-123',
                    'user_id': 'test-user',
                    'created_at': now.isoformat(),
                    'last_activity': now.isoformat(),
                    'data_uploads': [],
                    'investigation_history': []
                }
            else:
                # Second call fails
                raise Exception("Connection failed")

        mock_session_store.get.side_effect = get_side_effect
        mock_session_store.set.return_value = None

        success = await session_manager.update_session(
            "test-session-123", {"user_id": "updated-user"}
        )
        assert success is False

    @pytest.mark.asyncio
    async def test_add_data_upload_nonexistent_session(
        self, session_manager, mock_session_store
    ):
        """Test adding data upload to non-existent session."""
        mock_session_store.get.return_value = None

        success = await session_manager.add_data_upload(
            "non-existent-session", "data-123"
        )
        assert success is False

    @pytest.mark.asyncio
    async def test_add_investigation_history_nonexistent_session(
        self, session_manager, mock_session_store
    ):
        """Test adding investigation history to non-existent session."""
        mock_session_store.get.return_value = None

        success = await session_manager.add_investigation_history(
            "non-existent-session", {"type": "test", "message": "test message"}
        )
        assert success is False

    @pytest.mark.asyncio
    async def test_update_agent_state_nonexistent_session(
        self, session_manager, mock_session_store
    ):
        """Test updating agent state for non-existent session."""
        mock_session_store.get.return_value = None

        agent_state = AgentState(
            session_id="test-session",
            user_query="test query",
            current_phase="investigating",
            investigation_context={},
            findings=[],
            recommendations=[],
            confidence_score=0.8,
            tools_used=[],
        )

        success = await session_manager.update_agent_state(
            "non-existent-session", agent_state
        )
        assert success is False

    @pytest.mark.asyncio
    async def test_extend_session_nonexistent(
        self, session_manager, mock_session_store
    ):
        """Test extending non-existent session."""
        mock_session_store.get.return_value = None

        success = await session_manager.extend_session("non-existent-session")
        assert success is False

    @pytest.mark.asyncio
    async def test_validate_connection_success(self, session_manager, mock_session_store):
        """Test successful connection validation."""
        mock_session_store.set.return_value = None
        mock_session_store.delete.return_value = True
        
        result = await session_manager.validate_connection()
        assert result is True

    @pytest.mark.asyncio
    async def test_validate_connection_failure(self, session_manager, mock_session_store):
        """Test connection validation failure."""
        mock_session_store.set.side_effect = Exception("Connection failed")
        
        result = await session_manager.validate_connection()
        assert result is False

    @pytest.mark.asyncio
    async def test_close(self, session_manager):
        """Test SessionManager close method."""
        # Create a mock session store with redis_client attribute
        mock_redis_client = AsyncMock()
        mock_redis_client.aclose = AsyncMock()
        
        session_manager.session_store.redis_client = mock_redis_client
        
        await session_manager.close()
        
        # Should call aclose() on the redis client
        mock_redis_client.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_session_data_success(
        self, session_manager, mock_session_store, sample_session_data
    ):
        """Test successful session data cleanup."""
        # Add some data to cleanup
        session_with_data = sample_session_data.copy()
        session_with_data['data_uploads'] = ['data1', 'data2']
        session_with_data['investigation_history'] = [
            {'type': 'test', 'message': 'test1'},
            {'type': 'test', 'message': 'test2'}
        ]
        
        mock_session_store.get.return_value = session_with_data
        mock_session_store.set.return_value = None
        
        result = await session_manager.cleanup_session_data("test-session-123")
        
        assert result['success'] is True
        assert result['cleaned_items']['data_uploads'] == 2
        assert result['cleaned_items']['investigation_history'] == 2

    @pytest.mark.asyncio
    async def test_cleanup_session_data_nonexistent(
        self, session_manager, mock_session_store
    ):
        """Test cleanup of non-existent session."""
        mock_session_store.get.return_value = None
        
        result = await session_manager.cleanup_session_data("non-existent")
        
        assert result['success'] is False
        assert 'Session not found' in result['error']

    @pytest.mark.asyncio 
    async def test_cleanup_session_data_exception(
        self, session_manager, mock_session_store
    ):
        """Test cleanup when operation fails."""
        mock_session_store.get.side_effect = Exception("Connection failed")
        
        result = await session_manager.cleanup_session_data("test-session-123")
        
        assert result['success'] is False
        assert 'Connection failed' in result['error']