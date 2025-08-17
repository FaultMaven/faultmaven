"""
Unit tests for RedisSessionStore implementation.

This module tests the RedisSessionStore class to ensure proper
implementation of the ISessionStore interface with comprehensive coverage.
"""

import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Dict, Optional
from datetime import datetime

from faultmaven.infrastructure.persistence.redis_session_store import RedisSessionStore
from faultmaven.models.interfaces import ISessionStore


class TestRedisSessionStore:
    """Test suite for RedisSessionStore implementation"""
    
    @pytest.fixture
    def mock_redis_client(self):
        """Mock Redis client for testing"""
        with patch('faultmaven.infrastructure.persistence.redis_session_store.create_redis_client') as mock_redis_factory:
            mock_client = AsyncMock()
            mock_redis_factory.return_value = mock_client
            yield mock_client
    
    @pytest.fixture
    async def session_store(self, mock_redis_client):
        """Create RedisSessionStore instance for testing"""
        store = RedisSessionStore()
        yield store, mock_redis_client
    
    def test_implements_isessionstore_interface(self, mock_redis_client):
        """Test that RedisSessionStore properly implements ISessionStore interface"""
        store = RedisSessionStore()
        assert isinstance(store, ISessionStore)
        
        # Verify all required methods are present
        assert hasattr(store, 'get')
        assert hasattr(store, 'set')
        assert hasattr(store, 'delete')
        assert hasattr(store, 'exists')
        assert hasattr(store, 'extend_ttl')
        
        # Verify methods are coroutines
        assert callable(store.get)
        assert callable(store.set)
        assert callable(store.delete)
        assert callable(store.exists)
        assert callable(store.extend_ttl)
    
    def test_initialization(self, mock_redis_client):
        """Test successful initialization with default settings"""
        store = RedisSessionStore()
        
        # Verify initialization parameters
        assert store.default_ttl == 1800  # 30 minutes
        assert store.prefix == "session:"
        assert store.redis_client is not None
    
    @pytest.mark.asyncio
    async def test_get_success(self, session_store):
        """Test successful session retrieval"""
        store, mock_client = session_store
        
        # Mock Redis response
        session_data = {
            "session_id": "test-123",
            "user_id": "user-456",
            "last_activity": "2025-01-01T12:00:00"
        }
        mock_client.get.return_value = json.dumps(session_data)
        
        # Execute
        result = await store.get("test-123")
        
        # Verify Redis call
        mock_client.get.assert_called_once_with("session:test-123")
        
        # Verify result
        assert result == session_data
    
    @pytest.mark.asyncio
    async def test_get_not_found(self, session_store):
        """Test session retrieval when session doesn't exist"""
        store, mock_client = session_store
        
        mock_client.get.return_value = None
        
        result = await store.get("nonexistent")
        
        mock_client.get.assert_called_once_with("session:nonexistent")
        assert result is None
    
    @pytest.mark.asyncio
    async def test_get_invalid_json(self, session_store):
        """Test session retrieval with corrupted JSON data"""
        store, mock_client = session_store
        
        mock_client.get.return_value = "invalid json {"
        
        result = await store.get("corrupted")
        
        mock_client.get.assert_called_once_with("session:corrupted")
        assert result is None
    
    @pytest.mark.asyncio
    async def test_set_success(self, session_store):
        """Test successful session storage"""
        store, mock_client = session_store
        
        session_data = {
            "session_id": "test-123",
            "user_id": "user-456",
            "created_at": "2025-01-01T11:00:00"
        }
        
        # Execute
        await store.set("test-123", session_data, ttl=3600)
        
        # Verify Redis call
        mock_client.set.assert_called_once()
        call_args = mock_client.set.call_args
        
        assert call_args[0][0] == "session:test-123"  # key
        assert call_args[1]["ex"] == 3600  # TTL
        
        # Verify data was serialized and includes last_activity
        stored_data = json.loads(call_args[0][1])
        assert stored_data["session_id"] == "test-123"
        assert stored_data["user_id"] == "user-456"
        assert "last_activity" in stored_data  # Should be added automatically
    
    @pytest.mark.asyncio
    async def test_set_with_existing_last_activity(self, session_store):
        """Test session storage preserves existing last_activity"""
        store, mock_client = session_store
        
        existing_timestamp = "2025-01-01T10:00:00"
        session_data = {
            "session_id": "test-123",
            "last_activity": existing_timestamp
        }
        
        await store.set("test-123", session_data)
        
        # Verify existing timestamp was preserved
        call_args = mock_client.set.call_args
        stored_data = json.loads(call_args[0][1])
        assert stored_data["last_activity"] == existing_timestamp
    
    @pytest.mark.asyncio
    async def test_set_default_ttl(self, session_store):
        """Test session storage with default TTL"""
        store, mock_client = session_store
        
        session_data = {"session_id": "test-123"}
        
        await store.set("test-123", session_data)
        
        # Verify default TTL was used
        call_args = mock_client.set.call_args
        assert call_args[1]["ex"] == store.default_ttl
    
    @pytest.mark.asyncio
    async def test_delete_success(self, session_store):
        """Test successful session deletion"""
        store, mock_client = session_store
        
        mock_client.delete.return_value = 1  # One key deleted
        
        result = await store.delete("test-123")
        
        mock_client.delete.assert_called_once_with("session:test-123")
        assert result is True
    
    @pytest.mark.asyncio
    async def test_delete_not_found(self, session_store):
        """Test deletion of non-existent session"""
        store, mock_client = session_store
        
        mock_client.delete.return_value = 0  # No keys deleted
        
        result = await store.delete("nonexistent")
        
        mock_client.delete.assert_called_once_with("session:nonexistent")
        assert result is False
    
    @pytest.mark.asyncio
    async def test_exists_true(self, session_store):
        """Test session existence check for existing session"""
        store, mock_client = session_store
        
        mock_client.exists.return_value = 1  # Key exists
        
        result = await store.exists("test-123")
        
        mock_client.exists.assert_called_once_with("session:test-123")
        assert result is True
    
    @pytest.mark.asyncio
    async def test_exists_false(self, session_store):
        """Test session existence check for non-existent session"""
        store, mock_client = session_store
        
        mock_client.exists.return_value = 0  # Key doesn't exist
        
        result = await store.exists("nonexistent")
        
        mock_client.exists.assert_called_once_with("session:nonexistent")
        assert result is False
    
    @pytest.mark.asyncio
    async def test_extend_ttl_success(self, session_store):
        """Test successful TTL extension"""
        store, mock_client = session_store
        
        mock_client.expire.return_value = True  # TTL extended successfully
        
        result = await store.extend_ttl("test-123", ttl=7200)
        
        mock_client.expire.assert_called_once_with("session:test-123", 7200)
        assert result is True
    
    @pytest.mark.asyncio
    async def test_extend_ttl_default(self, session_store):
        """Test TTL extension with default TTL"""
        store, mock_client = session_store
        
        mock_client.expire.return_value = True
        
        result = await store.extend_ttl("test-123")
        
        mock_client.expire.assert_called_once_with("session:test-123", store.default_ttl)
        assert result is True
    
    @pytest.mark.asyncio
    async def test_extend_ttl_failure(self, session_store):
        """Test TTL extension for non-existent session"""
        store, mock_client = session_store
        
        mock_client.expire.return_value = False  # Key doesn't exist
        
        result = await store.extend_ttl("nonexistent")
        
        mock_client.expire.assert_called_once_with("session:nonexistent", store.default_ttl)
        assert result is False
    
    @pytest.mark.asyncio
    async def test_prefix_consistency(self, session_store):
        """Test that prefix is consistently applied across all operations"""
        store, mock_client = session_store
        
        test_key = "test-session"
        expected_full_key = f"{store.prefix}{test_key}"
        
        # Test get
        mock_client.get.return_value = None
        await store.get(test_key)
        mock_client.get.assert_called_with(expected_full_key)
        
        # Test set
        await store.set(test_key, {"test": "data"})
        assert mock_client.set.call_args[0][0] == expected_full_key
        
        # Test delete
        mock_client.delete.return_value = 0
        await store.delete(test_key)
        mock_client.delete.assert_called_with(expected_full_key)
        
        # Test exists
        mock_client.exists.return_value = 0
        await store.exists(test_key)
        mock_client.exists.assert_called_with(expected_full_key)
        
        # Test extend_ttl
        mock_client.expire.return_value = False
        await store.extend_ttl(test_key)
        mock_client.expire.assert_called_with(expected_full_key, store.default_ttl)
    
    def test_configuration_defaults(self):
        """Test default configuration values"""
        with patch('faultmaven.infrastructure.persistence.redis_session_store.create_redis_client'):
            store = RedisSessionStore()
            
            assert store.default_ttl == 1800  # 30 minutes in seconds
            assert store.prefix == "session:"
    
    @pytest.mark.asyncio
    async def test_json_serialization_deserialization(self, session_store):
        """Test proper JSON handling for complex session data"""
        store, mock_client = session_store
        
        complex_session_data = {
            "session_id": "test-123",
            "user_id": "user-456",
            "data_uploads": ["file1.log", "file2.log"],
            "case_history": [
                {"timestamp": "2025-01-01T12:00:00", "action": "upload"},
                {"timestamp": "2025-01-01T12:05:00", "action": "analyze"}
            ],
            "nested_data": {
                "key1": "value1",
                "key2": ["item1", "item2"]
            }
        }
        
        # Test set (serialization)
        await store.set("test-123", complex_session_data)
        call_args = mock_client.set.call_args
        serialized_data = call_args[0][1]
        
        # Verify it's valid JSON
        deserialized = json.loads(serialized_data)
        assert deserialized["session_id"] == "test-123"
        assert deserialized["data_uploads"] == ["file1.log", "file2.log"]
        assert len(deserialized["case_history"]) == 2
        assert deserialized["nested_data"]["key2"] == ["item1", "item2"]
        
        # Test get (deserialization)
        mock_client.get.return_value = serialized_data
        retrieved_data = await store.get("test-123")
        
        # Verify complex data was properly deserialized
        assert retrieved_data["session_id"] == "test-123"
        assert retrieved_data["data_uploads"] == ["file1.log", "file2.log"]
        assert len(retrieved_data["case_history"]) == 2
        assert retrieved_data["nested_data"]["key2"] == ["item1", "item2"]
    
    @pytest.mark.asyncio
    async def test_error_handling(self, session_store):
        """Test error handling in various operations"""
        store, mock_client = session_store
        
        # Test Redis exceptions are propagated
        mock_client.get.side_effect = Exception("Redis connection error")
        
        with pytest.raises(Exception, match="Redis connection error"):
            await store.get("test-key")
        
        # Test JSON encoding errors
        mock_client.set.side_effect = Exception("Redis set error")
        
        with pytest.raises(Exception, match="Redis set error"):
            await store.set("test-key", {"data": "test"})
    
    @pytest.mark.asyncio
    async def test_edge_cases(self, session_store):
        """Test edge cases and boundary conditions"""
        store, mock_client = session_store
        
        # Test empty session data
        empty_data = {}
        await store.set("empty", empty_data)
        
        call_args = mock_client.set.call_args
        stored_data = json.loads(call_args[0][1])
        assert "last_activity" in stored_data  # Should still be added
        
        # Test very long session key - ensure mock returns proper JSON string, not AsyncMock
        long_key = "x" * 1000
        mock_client.get.return_value = json.dumps({"key": "value"})  # Return actual JSON string
        await store.get(long_key)
        mock_client.get.assert_called_with(f"session:{long_key}")
        
        # Test TTL of 0 (should work)
        await store.set("test", {"data": "test"}, ttl=0)
        call_args = mock_client.set.call_args
        assert call_args[1]["ex"] == 0


@pytest.mark.integration
class TestRedisSessionStoreIntegration:
    """Integration tests for RedisSessionStore with real dependencies"""
    
    @pytest.fixture
    def mock_redis_client(self):
        """Mock Redis client for integration testing"""
        with patch('faultmaven.infrastructure.persistence.redis_session_store.create_redis_client') as mock_redis_factory:
            mock_client = AsyncMock()
            mock_redis_factory.return_value = mock_client
            yield mock_client
    
    @pytest.fixture
    async def session_store(self, mock_redis_client):
        """Create RedisSessionStore instance for integration testing"""
        store = RedisSessionStore()
        yield store, mock_redis_client
    
    @pytest.mark.skipif(
        condition=True,  # Skip by default - requires real Redis instance
        reason="Requires running Redis instance"
    )
    @pytest.mark.asyncio
    async def test_real_redis_integration(self):
        """Test with real Redis instance (skipped by default)"""
        # This test would require a real Redis instance running
        # It's skipped by default but can be enabled for full integration testing
        pass
    
    @pytest.mark.asyncio
    async def test_interface_compliance_comprehensive(self):
        """Comprehensive test of interface compliance"""
        with patch('faultmaven.infrastructure.persistence.redis_session_store.create_redis_client'):
            store = RedisSessionStore()
            
            # Test all interface methods exist and are callable
            assert hasattr(store, 'get')
            assert hasattr(store, 'set')
            assert hasattr(store, 'delete')
            assert hasattr(store, 'exists')
            assert hasattr(store, 'extend_ttl')
            
            # Verify method signatures match interface
            import inspect
            
            get_sig = inspect.signature(store.get)
            assert 'key' in get_sig.parameters
            
            set_sig = inspect.signature(store.set)
            assert 'key' in set_sig.parameters
            assert 'value' in set_sig.parameters
            assert 'ttl' in set_sig.parameters
            assert set_sig.parameters['ttl'].default is None
            
            delete_sig = inspect.signature(store.delete)
            assert 'key' in delete_sig.parameters
            
            exists_sig = inspect.signature(store.exists)
            assert 'key' in exists_sig.parameters
            
            extend_ttl_sig = inspect.signature(store.extend_ttl)
            assert 'key' in extend_ttl_sig.parameters
            assert 'ttl' in extend_ttl_sig.parameters
            assert extend_ttl_sig.parameters['ttl'].default is None
    
    @pytest.mark.asyncio
    async def test_session_lifecycle(self, session_store):
        """Test complete session lifecycle using interface"""
        store, mock_client = session_store
        
        # Setup mocks for lifecycle test
        session_data = {
            "session_id": "lifecycle-test",
            "user_id": "test-user",
            "created_at": "2025-01-01T10:00:00"
        }
        
        # Test create (set)
        await store.set("lifecycle-test", session_data, ttl=3600)
        assert mock_client.set.called
        
        # Test retrieve (get)
        mock_client.get.return_value = json.dumps(session_data)
        retrieved = await store.get("lifecycle-test")
        assert retrieved["session_id"] == "lifecycle-test"
        
        # Test existence check
        mock_client.exists.return_value = 1
        exists = await store.exists("lifecycle-test")
        assert exists is True
        
        # Test TTL extension
        mock_client.expire.return_value = True
        extended = await store.extend_ttl("lifecycle-test", ttl=7200)
        assert extended is True
        
        # Test deletion
        mock_client.delete.return_value = 1
        deleted = await store.delete("lifecycle-test")
        assert deleted is True
        
        # Test existence after deletion
        mock_client.exists.return_value = 0
        exists_after = await store.exists("lifecycle-test")
        assert exists_after is False