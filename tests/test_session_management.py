import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from faultmaven.models import AgentState, SessionContext
from faultmaven.session_management import SessionManager


@pytest.fixture
def sample_session_context():
    """Create a sample session context for testing."""
    return SessionContext(
        session_id="test-session-123",
        user_id="test-user",
        agent_state={
            "session_id": "test-session-123",
            "user_query": "Test query",
            "current_phase": "initial",
            "investigation_context": {},
            "findings": [],
            "recommendations": [],
            "confidence_score": 0.5,
            "tools_used": [],
        },
    )


@pytest.fixture
def mock_redis():
    """Create a mock Redis client for testing."""
    redis_mock = AsyncMock()
    redis_mock.set = AsyncMock()
    redis_mock.get = AsyncMock()
    redis_mock.delete = AsyncMock()
    redis_mock.exists = AsyncMock()
    redis_mock.keys = AsyncMock()
    redis_mock.close = AsyncMock()
    redis_mock.expire = AsyncMock()
    return redis_mock


@pytest.fixture
def session_manager_with_mock_redis(mock_redis):
    """Create a SessionManager with mock Redis client."""
    with patch("faultmaven.infrastructure.redis_client.create_redis_client", return_value=mock_redis):
        session_manager = SessionManager(redis_url="redis://localhost:6379")
        # Ensure the mock is properly assigned
        session_manager.redis_client = mock_redis
        return session_manager


class TestSessionManager:
    """Test suite for SessionManager class."""

    def test_init_default_configuration(self):
        """Test SessionManager initialization with default configuration."""
        with patch("faultmaven.infrastructure.redis_client.create_redis_client") as mock_create_client:
            mock_create_client.return_value = AsyncMock()
            session_manager = SessionManager()
            assert session_manager.session_timeout == timedelta(
                hours=24
            )  # 24 hours default
            assert session_manager.session_timeout_seconds == 24 * 3600

    def test_init_custom_configuration(self):
        """Test SessionManager initialization with custom configuration."""
        with patch("faultmaven.infrastructure.redis_client.create_redis_client") as mock_create_client:
            mock_create_client.return_value = AsyncMock()
            session_manager = SessionManager(
                redis_url="redis://test:6379", session_timeout_hours=2
            )
            assert session_manager.session_timeout == timedelta(hours=2)
            assert session_manager.session_timeout_seconds == 2 * 3600

    @pytest.mark.asyncio
    async def test_create_session_success(
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test successful session creation."""
        session_manager = session_manager_with_mock_redis
        mock_redis.set.return_value = True

        session = await session_manager.create_session("test-user")

        assert isinstance(session, SessionContext)
        assert session.user_id == "test-user"
        mock_redis.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_session_with_existing_user(
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test creating multiple sessions for the same user."""
        session_manager = session_manager_with_mock_redis
        mock_redis.set.return_value = True

        session1 = await session_manager.create_session("test-user")
        session2 = await session_manager.create_session("test-user")

        assert session1.session_id != session2.session_id
        assert session1.user_id == session2.user_id == "test-user"
        assert mock_redis.set.call_count == 2

    @pytest.mark.asyncio
    async def test_get_session_existing(
        self, session_manager_with_mock_redis, mock_redis, sample_session_context
    ):
        """Test retrieving an existing session."""
        session_manager = session_manager_with_mock_redis

        # Mock Redis returning session data
        mock_redis.get.return_value = sample_session_context.model_dump_json()
        mock_redis.set.return_value = True

        retrieved_session = await session_manager.get_session("test-session-123")

        assert retrieved_session is not None
        assert retrieved_session.session_id == "test-session-123"
        mock_redis.get.assert_called_once_with("session:test-session-123")

    @pytest.mark.asyncio
    async def test_get_session_nonexistent(
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test retrieving a non-existent session."""
        session_manager = session_manager_with_mock_redis
        mock_redis.get.return_value = None

        session = await session_manager.get_session("non-existent-session")

        assert session is None

    @pytest.mark.asyncio
    async def test_update_session_success(
        self, session_manager_with_mock_redis, mock_redis, sample_session_context
    ):
        """Test successful session update."""
        session_manager = session_manager_with_mock_redis

        # Mock getting the session first
        mock_redis.get.return_value = sample_session_context.model_dump_json()
        mock_redis.set.return_value = True

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
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test updating a non-existent session."""
        session_manager = session_manager_with_mock_redis
        mock_redis.get.return_value = None

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
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test deleting an existing session."""
        session_manager = session_manager_with_mock_redis
        mock_redis.delete.return_value = 1  # Redis delete returns count of deleted keys

        success = await session_manager.delete_session("test-session-123")

        assert success is True
        mock_redis.delete.assert_called_once_with("session:test-session-123")

    @pytest.mark.asyncio
    async def test_delete_session_nonexistent(
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test deleting a non-existent session."""
        session_manager = session_manager_with_mock_redis
        mock_redis.delete.return_value = 0  # No keys deleted

        success = await session_manager.delete_session("non-existent-session")
        assert success is False

    @pytest.mark.asyncio
    async def test_add_conversation_message(
        self, session_manager_with_mock_redis, mock_redis, sample_session_context
    ):
        """Test adding conversation message to session."""
        session_manager = session_manager_with_mock_redis

        # Mock getting the session first
        mock_redis.get.return_value = sample_session_context.model_dump_json()
        mock_redis.set.return_value = True

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
        self, session_manager_with_mock_redis, mock_redis, sample_session_context
    ):
        """Test adding uploaded data to session."""
        session_manager = session_manager_with_mock_redis

        # Mock getting the session first
        mock_redis.get.return_value = sample_session_context.model_dump_json()
        mock_redis.set.return_value = True

        success = await session_manager.add_data_upload("test-session-123", "data-123")
        assert success is True

    @pytest.mark.asyncio
    async def test_list_sessions(self, session_manager_with_mock_redis, mock_redis):
        """Test listing sessions."""
        session_manager = session_manager_with_mock_redis

        # Create test sessions
        session1 = SessionContext(session_id="session1", user_id="user1")
        session2 = SessionContext(session_id="session2", user_id="user2")
        session3 = SessionContext(session_id="session3", user_id="user1")

        # Mock Redis returning session keys and data
        mock_redis.keys.return_value = [
            "session:session1",
            "session:session2",
            "session:session3",
        ]
        mock_redis.get.side_effect = [
            session1.model_dump_json(),
            session2.model_dump_json(),
            session3.model_dump_json(),
        ]

        all_sessions = await session_manager.list_sessions()
        assert len(all_sessions) == 3

        # Reset mock for user-specific query
        mock_redis.keys.return_value = [
            "session:session1",
            "session:session2",
            "session:session3",
        ]
        mock_redis.get.side_effect = [
            session1.model_dump_json(),
            session2.model_dump_json(),
            session3.model_dump_json(),
        ]

        user1_sessions = await session_manager.list_sessions("user1")
        assert len(user1_sessions) == 2
        assert all(s.user_id == "user1" for s in user1_sessions)

    @pytest.mark.asyncio
    async def test_get_session_stats(self, session_manager_with_mock_redis, mock_redis):
        """Test getting session statistics."""
        session_manager = session_manager_with_mock_redis

        # Create test sessions with different creation times
        now = datetime.utcnow()
        session1 = SessionContext(
            session_id="session1", user_id="user1", created_at=now, last_activity=now
        )
        session2 = SessionContext(
            session_id="session2", user_id="user2", created_at=now, last_activity=now
        )

        # Mock Redis returning session keys and data
        mock_redis.keys.return_value = ["session:session1", "session:session2"]
        mock_redis.get.side_effect = [
            session1.model_dump_json(),
            session2.model_dump_json(),
        ]

        stats = await session_manager.get_session_stats()
        assert stats["total_sessions"] == 2
        assert stats["active_sessions"] == 2
        assert "user1" in stats["sessions_by_user"]
        assert "user2" in stats["sessions_by_user"]

    @pytest.mark.asyncio
    async def test_is_session_active(self, session_manager_with_mock_redis, mock_redis):
        """Test checking if a session is active."""
        session_manager = session_manager_with_mock_redis

        # Test active session
        mock_redis.exists.return_value = 1
        assert await session_manager.is_session_active("test-session-123") is True

        # Test inactive session
        mock_redis.exists.return_value = 0
        assert await session_manager.is_session_active("inactive-session") is False

    @pytest.mark.asyncio
    async def test_extend_session(
        self, session_manager_with_mock_redis, mock_redis, sample_session_context
    ):
        """Test extending session timeout."""
        session_manager = session_manager_with_mock_redis

        # Mock getting the session first
        mock_redis.get.return_value = sample_session_context.model_dump_json()
        mock_redis.set.return_value = True

        success = await session_manager.extend_session("test-session-123")
        assert success is True

    @pytest.mark.asyncio
    async def test_session_lifecycle(self, session_manager_with_mock_redis, mock_redis):
        """Test complete session lifecycle."""
        session_manager = session_manager_with_mock_redis

        # Mock successful Redis operations
        mock_redis.set.return_value = True
        mock_redis.delete.return_value = 1

        # Create session
        session = await session_manager.create_session("test-user")
        assert session is not None

        # Mock getting the session for update
        mock_redis.get.return_value = session.model_dump_json()

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

    def test_session_serialization(self, sample_session_context):
        """Test session serialization and deserialization."""
        # Test that session can be serialized to dict
        session_dict = sample_session_context.model_dump()
        assert session_dict["session_id"] == sample_session_context.session_id
        assert session_dict["user_id"] == sample_session_context.user_id

        # Test that session can be created from dict
        new_session = SessionContext(**session_dict)
        assert new_session.session_id == sample_session_context.session_id
        assert new_session.user_id == sample_session_context.user_id

    @pytest.mark.asyncio
    async def test_redis_connection_close(self):
        """Test closing Redis connection."""
        # Use the existing fixture pattern
        mock_redis = AsyncMock()
        mock_redis.aclose = AsyncMock()
        mock_redis.close = AsyncMock()
        
        # Directly assign the mock to bypass the factory
        session_manager = SessionManager(redis_url="redis://localhost:6379")
        session_manager.redis_client = mock_redis
        
        await session_manager.close()
        
        # Should call aclose() when available
        mock_redis.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_corrupted_session_cleanup(
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test that corrupted session data is cleaned up."""
        session_manager = session_manager_with_mock_redis

        # Mock Redis returning invalid JSON
        mock_redis.get.return_value = "invalid json data"
        mock_redis.delete.return_value = 1

        session = await session_manager.get_session("corrupted-session")

        assert session is None
        # Should attempt to clean up corrupted session
        mock_redis.delete.assert_called_with("session:corrupted-session")

    @pytest.mark.asyncio
    async def test_concurrent_session_updates_race_condition(
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test race condition handling when multiple updates happen simultaneously."""
        session_manager = session_manager_with_mock_redis

        # Create a base session
        base_session = SessionContext(
            session_id="race-test-session",
            user_id="test-user",
            data_uploads=["data1"],
            investigation_history=[{"type": "initial", "message": "start"}],
        )

        # Mock Redis to simulate concurrent access
        mock_redis.get.return_value = base_session.model_dump_json()
        mock_redis.set.return_value = True

        # Simulate concurrent updates using asyncio.gather
        async def update_data_upload():
            return await session_manager.add_data_upload("race-test-session", "data2")

        async def update_investigation_history():
            return await session_manager.add_investigation_history(
                "race-test-session", {"type": "concurrent", "message": "update1"}
            )

        async def update_agent_state():
            return await session_manager.update_agent_state(
                "race-test-session",
                AgentState(
                    session_id="race-test-session",
                    user_query="concurrent query",
                    current_phase="investigating",
                    investigation_context={},
                    findings=[],
                    recommendations=[],
                    confidence_score=0.8,
                    tools_used=[],
                ),
            )

        # Execute all updates concurrently
        results = await asyncio.gather(
            update_data_upload(),
            update_investigation_history(),
            update_agent_state(),
            return_exceptions=True,
        )

        # All operations should succeed even with concurrent access
        assert all(result is True for result in results)

        # Verify Redis was called for each operation
        assert mock_redis.set.call_count >= 3

    @pytest.mark.asyncio
    async def test_session_update_last_activity(
        self, session_manager_with_mock_redis, mock_redis, sample_session_context
    ):
        """Test updating last activity for a session."""
        session_manager = session_manager_with_mock_redis

        # Mock getting the session
        mock_redis.get.return_value = sample_session_context.model_dump_json()
        mock_redis.set.return_value = True

        success = await session_manager.update_last_activity("test-session-123")
        assert success is True
        # The session is updated twice: once in get_session and once in update_last_activity
        assert mock_redis.set.call_count >= 1

    @pytest.mark.asyncio
    async def test_session_update_last_activity_nonexistent(
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test updating last activity for non-existent session."""
        session_manager = session_manager_with_mock_redis

        # Mock Redis returning None (session not found)
        mock_redis.get.return_value = None

        success = await session_manager.update_last_activity("non-existent-session")
        assert success is False

    @pytest.mark.asyncio
    async def test_session_update_last_activity_exception(
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test updating last activity when Redis operation fails."""
        session_manager = session_manager_with_mock_redis

        # Mock Redis raising an exception
        mock_redis.get.side_effect = Exception("Redis connection failed")

        success = await session_manager.update_last_activity("test-session-123")
        assert success is False

    @pytest.mark.asyncio
    async def test_list_sessions_with_parsing_errors(
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test listing sessions when some session data is corrupted."""
        session_manager = session_manager_with_mock_redis

        # Create valid session
        valid_session = SessionContext(session_id="valid-session", user_id="user1")

        # Mock Redis returning mix of valid and invalid data
        mock_redis.keys.return_value = [
            "session:valid-session",
            "session:corrupted-session",
        ]
        mock_redis.get.side_effect = [
            valid_session.model_dump_json(),  # Valid session
            "invalid json data",  # Corrupted session
        ]

        sessions = await session_manager.list_sessions()

        # Should return only valid sessions, skip corrupted ones
        assert len(sessions) == 1
        assert sessions[0].session_id == "valid-session"

    @pytest.mark.asyncio
    async def test_list_sessions_redis_exception(
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test listing sessions when Redis operation fails."""
        session_manager = session_manager_with_mock_redis

        # Mock Redis raising an exception
        mock_redis.keys.side_effect = Exception("Redis connection failed")

        sessions = await session_manager.list_sessions()
        assert sessions == []

    @pytest.mark.asyncio
    async def test_get_session_stats_empty_sessions(
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test getting session stats when no sessions exist."""
        session_manager = session_manager_with_mock_redis

        # Mock Redis returning no sessions
        mock_redis.keys.return_value = []

        stats = await session_manager.get_session_stats()

        assert stats["total_sessions"] == 0
        assert stats["active_sessions"] == 0
        assert stats["average_duration_hours"] == 0
        assert stats["sessions_by_user"] == {}
        assert stats["oldest_session"] is None
        assert stats["newest_session"] is None

    @pytest.mark.asyncio
    async def test_get_session_stats_with_old_sessions(
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test getting session stats with sessions of varying ages."""
        session_manager = session_manager_with_mock_redis

        # Create sessions with different creation times
        now = datetime.utcnow()
        old_time = now - timedelta(hours=3)  # 3 hours ago
        recent_time = now - timedelta(minutes=30)  # 30 minutes ago

        old_session = SessionContext(
            session_id="old-session",
            user_id="user1",
            created_at=old_time,
            last_activity=old_time,
        )
        recent_session = SessionContext(
            session_id="recent-session",
            user_id="user2",
            created_at=recent_time,
            last_activity=recent_time,
        )

        # Mock Redis returning both sessions
        mock_redis.keys.return_value = ["session:old-session", "session:recent-session"]
        mock_redis.get.side_effect = [
            old_session.model_dump_json(),
            recent_session.model_dump_json(),
        ]

        stats = await session_manager.get_session_stats()

        assert stats["total_sessions"] == 2
        assert stats["active_sessions"] == 1  # Only recent session is active
        assert "user1" in stats["sessions_by_user"]
        assert "user2" in stats["sessions_by_user"]
        assert stats["oldest_session"] == old_time
        assert stats["newest_session"] == recent_time

    @pytest.mark.asyncio
    async def test_delete_session_redis_exception(
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test deleting session when Redis operation fails."""
        session_manager = session_manager_with_mock_redis

        # Mock Redis raising an exception
        mock_redis.delete.side_effect = Exception("Redis connection failed")

        success = await session_manager.delete_session("test-session-123")
        assert success is False

    @pytest.mark.asyncio
    async def test_update_session_nonexistent_redis_exception(
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test updating a non-existent session with Redis exception handling."""
        session_manager = session_manager_with_mock_redis

        # Mock Redis returning None (session not found)
        mock_redis.get.return_value = None

        success = await session_manager.update_session(
            "non-existent-session", {"user_id": "new-user"}
        )
        assert success is False

    @pytest.mark.asyncio
    async def test_update_session_redis_exception(
        self, session_manager_with_mock_redis, mock_redis, sample_session_context
    ):
        """Test updating session when Redis operation fails."""
        session_manager = session_manager_with_mock_redis

        # Mock getting session successfully but setting fails
        mock_redis.get.return_value = sample_session_context.model_dump_json()
        mock_redis.set.side_effect = Exception("Redis connection failed")

        success = await session_manager.update_session(
            "test-session-123", {"user_id": "updated-user"}
        )
        assert success is False

    @pytest.mark.asyncio
    async def test_add_data_upload_nonexistent_session(
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test adding data upload to non-existent session."""
        session_manager = session_manager_with_mock_redis

        # Mock Redis returning None (session not found)
        mock_redis.get.return_value = None

        success = await session_manager.add_data_upload(
            "non-existent-session", "data-123"
        )
        assert success is False

    @pytest.mark.asyncio
    async def test_add_investigation_history_nonexistent_session(
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test adding investigation history to non-existent session."""
        session_manager = session_manager_with_mock_redis

        # Mock Redis returning None (session not found)
        mock_redis.get.return_value = None

        success = await session_manager.add_investigation_history(
            "non-existent-session", {"type": "test", "message": "test message"}
        )
        assert success is False

    @pytest.mark.asyncio
    async def test_update_agent_state_nonexistent_session(
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test updating agent state for non-existent session."""
        session_manager = session_manager_with_mock_redis

        # Mock Redis returning None (session not found)
        mock_redis.get.return_value = None

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
        self, session_manager_with_mock_redis, mock_redis
    ):
        """Test extending non-existent session."""
        session_manager = session_manager_with_mock_redis

        # Mock Redis returning None (session not found)
        mock_redis.get.return_value = None

        success = await session_manager.extend_session("non-existent-session")
        assert success is False
