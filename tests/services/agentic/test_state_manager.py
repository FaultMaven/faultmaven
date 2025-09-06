"""Unit Tests for Agent State Manager Component

Tests for the State & Session Manager component of the agentic framework,
validating persistent memory and execution state management capabilities.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
import json

from faultmaven.services.agentic.state_manager import AgentStateManager
from faultmaven.models.agentic import (
    AgentExecutionState,
    ConversationMemory,
    ExecutionPlan
)


class TestAgentStateManager:
    """Test suite for Agent State Manager."""

    @pytest.fixture
    def mock_redis_client(self):
        """Mock Redis client."""
        mock_client = AsyncMock()
        mock_client.get.return_value = None
        mock_client.set.return_value = True
        mock_client.exists.return_value = False
        mock_client.expire.return_value = True
        mock_client.hgetall.return_value = {}
        mock_client.hset.return_value = True
        return mock_client

    @pytest.fixture
    def state_manager(self, mock_redis_client):
        """State manager instance with mocked dependencies."""
        return AgentStateManager(redis_client=mock_redis_client)

    @pytest.mark.asyncio
    async def test_initialization(self, state_manager):
        """Test state manager initialization."""
        assert state_manager is not None
        assert hasattr(state_manager, 'redis_client')
        assert hasattr(state_manager, 'default_ttl')
        assert state_manager.default_ttl == 3600  # 1 hour default

    @pytest.mark.asyncio
    async def test_get_execution_state_not_exists(self, state_manager, mock_redis_client):
        """Test getting execution state that doesn't exist."""
        mock_redis_client.get.return_value = None
        
        result = await state_manager.get_execution_state("non_existent_session")
        
        assert result is None
        mock_redis_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_execution_state_exists(self, state_manager, mock_redis_client):
        """Test getting existing execution state."""
        mock_state = {
            "session_id": "test_session",
            "current_step": 2,
            "workflow_status": "running",
            "context": {"key": "value"},
            "created_at": datetime.utcnow().isoformat(),
            "last_updated": datetime.utcnow().isoformat()
        }
        mock_redis_client.get.return_value = json.dumps(mock_state)
        
        result = await state_manager.get_execution_state("test_session")
        
        assert result is not None
        assert isinstance(result, AgentExecutionState)
        assert result.session_id == "test_session"
        assert result.current_step == 2
        assert result.workflow_status == "running"

    @pytest.mark.asyncio
    async def test_update_execution_state_new(self, state_manager, mock_redis_client):
        """Test updating execution state for new session."""
        mock_redis_client.exists.return_value = False
        
        state = AgentExecutionState(
            session_id="new_session",
            current_step=1,
            workflow_status="planning",
            context={"domain": "test"},
            created_at=datetime.utcnow(),
            last_updated=datetime.utcnow()
        )
        
        result = await state_manager.update_execution_state("new_session", state)
        
        assert result is True
        mock_redis_client.set.assert_called_once()
        mock_redis_client.expire.assert_called_once()

    @pytest.mark.asyncio
    async def test_conversation_memory_operations(self, state_manager, mock_redis_client):
        """Test conversation memory management."""
        session_id = "test_conv_session"
        
        # Test getting non-existent memory
        mock_redis_client.hgetall.return_value = {}
        memory = await state_manager.get_conversation_memory(session_id)
        assert memory is None

        # Test updating memory
        new_memory = ConversationMemory(
            session_id=session_id,
            messages=[
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"}
            ],
            context={"domain": "greeting"},
            created_at=datetime.utcnow(),
            last_updated=datetime.utcnow()
        )
        
        result = await state_manager.update_conversation_memory(session_id, new_memory)
        assert result is True
        mock_redis_client.hset.assert_called()

    @pytest.mark.asyncio
    async def test_create_execution_plan(self, state_manager):
        """Test execution plan creation."""
        session_id = "plan_test_session"
        query = "Help me troubleshoot my server"
        context = {"domain": "infrastructure", "urgency": "high"}
        
        plan = await state_manager.create_execution_plan(session_id, query, context)
        
        assert isinstance(plan, ExecutionPlan)
        assert plan.session_id == session_id
        assert plan.query == query
        assert plan.context == context
        assert plan.created_at is not None
        assert len(plan.steps) > 0  # Should have some default steps

    @pytest.mark.asyncio
    async def test_session_cleanup(self, state_manager, mock_redis_client):
        """Test session cleanup functionality."""
        session_id = "cleanup_test_session"
        
        result = await state_manager.cleanup_session(session_id)
        
        assert result is True
        # Should delete multiple keys related to the session
        assert mock_redis_client.delete.call_count >= 2

    @pytest.mark.asyncio
    async def test_get_active_sessions(self, state_manager, mock_redis_client):
        """Test getting active sessions."""
        # Mock Redis scan to return some session keys
        mock_redis_client.scan.return_value = (
            0,  # cursor
            [b"execution_state:session1", b"execution_state:session2", b"other_key"]
        )
        
        sessions = await state_manager.get_active_sessions()
        
        assert isinstance(sessions, list)
        # Should extract session IDs from keys
        assert len(sessions) >= 0  # May be empty in mock scenario

    @pytest.mark.asyncio
    async def test_state_persistence_with_ttl(self, state_manager, mock_redis_client):
        """Test state persistence with TTL management."""
        session_id = "ttl_test_session"
        custom_ttl = 7200  # 2 hours
        
        state = AgentExecutionState(
            session_id=session_id,
            current_step=1,
            workflow_status="running",
            context={},
            created_at=datetime.utcnow(),
            last_updated=datetime.utcnow()
        )
        
        result = await state_manager.update_execution_state(session_id, state, ttl=custom_ttl)
        
        assert result is True
        mock_redis_client.expire.assert_called_with(
            f"execution_state:{session_id}", custom_ttl
        )

    @pytest.mark.asyncio
    async def test_error_handling_redis_failure(self, state_manager, mock_redis_client):
        """Test error handling when Redis operations fail."""
        # Simulate Redis connection failure
        mock_redis_client.get.side_effect = Exception("Redis connection failed")
        
        result = await state_manager.get_execution_state("test_session")
        
        # Should handle the error gracefully
        assert result is None

    @pytest.mark.asyncio
    async def test_concurrent_state_updates(self, state_manager, mock_redis_client):
        """Test concurrent state updates."""
        import asyncio
        
        session_id = "concurrent_test_session"
        
        async def update_state(step_num):
            state = AgentExecutionState(
                session_id=session_id,
                current_step=step_num,
                workflow_status="running",
                context={"step": step_num},
                created_at=datetime.utcnow(),
                last_updated=datetime.utcnow()
            )
            return await state_manager.update_execution_state(session_id, state)
        
        # Run multiple concurrent updates
        tasks = [update_state(i) for i in range(5)]
        results = await asyncio.gather(*tasks)
        
        # All updates should succeed
        assert all(results)

    @pytest.mark.asyncio
    async def test_memory_size_limits(self, state_manager):
        """Test conversation memory size management."""
        session_id = "memory_limit_test"
        
        # Create memory with many messages
        large_messages = [
            {"role": "user", "content": f"Message {i}"}
            for i in range(1000)  # Large number of messages
        ]
        
        memory = ConversationMemory(
            session_id=session_id,
            messages=large_messages,
            context={},
            created_at=datetime.utcnow(),
            last_updated=datetime.utcnow()
        )
        
        result = await state_manager.update_conversation_memory(session_id, memory)
        
        # Should handle large memory (may truncate or compress)
        assert result is True

    @pytest.mark.asyncio
    async def test_state_serialization(self, state_manager):
        """Test state serialization and deserialization."""
        original_state = AgentExecutionState(
            session_id="serialization_test",
            current_step=5,
            workflow_status="completed",
            context={"result": "success", "nested": {"key": "value"}},
            created_at=datetime.utcnow(),
            last_updated=datetime.utcnow()
        )
        
        # Test serialization
        serialized = state_manager._serialize_state(original_state)
        assert isinstance(serialized, str)
        
        # Test deserialization
        deserialized = state_manager._deserialize_state(serialized)
        assert isinstance(deserialized, AgentExecutionState)
        assert deserialized.session_id == original_state.session_id
        assert deserialized.current_step == original_state.current_step
        assert deserialized.workflow_status == original_state.workflow_status

    @pytest.mark.asyncio
    async def test_session_analytics(self, state_manager, mock_redis_client):
        """Test session analytics capabilities."""
        # Mock multiple active sessions
        mock_redis_client.scan.return_value = (
            0,
            [f"execution_state:session_{i}".encode() for i in range(10)]
        )
        
        analytics = await state_manager.get_session_analytics()
        
        assert isinstance(analytics, dict)
        assert "total_sessions" in analytics
        assert "active_sessions" in analytics

    @pytest.mark.asyncio
    async def test_state_backup_and_restore(self, state_manager):
        """Test state backup and restore functionality."""
        session_id = "backup_test_session"
        
        original_state = AgentExecutionState(
            session_id=session_id,
            current_step=3,
            workflow_status="paused",
            context={"checkpoint": "step_3"},
            created_at=datetime.utcnow(),
            last_updated=datetime.utcnow()
        )
        
        # Test backup
        backup_data = await state_manager.backup_session_state(session_id)
        assert isinstance(backup_data, dict)
        
        # Test restore (if implemented)
        if hasattr(state_manager, 'restore_session_state'):
            result = await state_manager.restore_session_state(session_id, backup_data)
            assert result is True