"""
Unit tests for RedisSessionStore implementation.

Tests use FakeRedis — the same Redis-compatible client used in local deployment.
This ensures tests exercise the exact same code path as production.
"""

import inspect
import json

import pytest

from faultmaven.models.interfaces import ISessionStore
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)


@pytest.fixture
def redis_client():
    """Create a FakeRedis client for testing."""
    import fakeredis.aioredis as fakeredis_aio

    return fakeredis_aio.FakeRedis(decode_responses=True)


@pytest.fixture
def session_store(redis_client):
    """Create RedisSessionStore backed by FakeRedis."""
    return RedisSessionStore(redis_client)


class TestRedisSessionStore:
    """Test suite for RedisSessionStore implementation"""

    def test_implements_isessionstore_interface(self, session_store):
        """Test that RedisSessionStore properly implements ISessionStore interface"""
        assert isinstance(session_store, ISessionStore)

        assert callable(session_store.get)
        assert callable(session_store.set)
        assert callable(session_store.delete)
        assert callable(session_store.exists)
        assert callable(session_store.extend_ttl)

    def test_initialization(self, redis_client):
        """Test successful initialization"""
        store = RedisSessionStore(redis_client)

        assert store.default_ttl == 1800  # 30 minutes
        assert store.prefix == "session:"
        assert store.redis_client is redis_client

    @pytest.mark.asyncio
    async def test_get_success(self, session_store, redis_client):
        """Test successful session retrieval"""
        session_data = {
            "session_id": "test-123",
            "user_id": "user-456",
            "last_activity": "2025-01-01T12:00:00",
        }
        await redis_client.set("session:test-123", json.dumps(session_data), ex=1800)

        result = await session_store.get("test-123")

        assert result == session_data

    @pytest.mark.asyncio
    async def test_get_not_found(self, session_store):
        """Test session retrieval when session doesn't exist"""
        result = await session_store.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_invalid_json(self, session_store, redis_client):
        """Test session retrieval with corrupted JSON data"""
        await redis_client.set("session:corrupted", "invalid json {", ex=60)

        result = await session_store.get("corrupted")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_success(self, session_store, redis_client):
        """Test successful session storage"""
        session_data = {
            "session_id": "test-123",
            "user_id": "user-456",
            "created_at": "2025-01-01T11:00:00",
        }

        await session_store.set("test-123", session_data, ttl=3600)

        # Verify data was stored in Redis
        raw = await redis_client.get("session:test-123")
        stored_data = json.loads(raw)
        assert stored_data["session_id"] == "test-123"
        assert stored_data["user_id"] == "user-456"
        assert "last_activity" in stored_data  # Auto-added

        # Verify TTL was set
        ttl = await redis_client.ttl("session:test-123")
        assert ttl > 0
        assert ttl <= 3600

    @pytest.mark.asyncio
    async def test_set_with_existing_last_activity(self, session_store, redis_client):
        """Test session storage preserves existing last_activity"""
        existing_timestamp = "2025-01-01T10:00:00"
        session_data = {"session_id": "test-123", "last_activity": existing_timestamp}

        await session_store.set("test-123", session_data)

        raw = await redis_client.get("session:test-123")
        stored_data = json.loads(raw)
        assert stored_data["last_activity"] == existing_timestamp

    @pytest.mark.asyncio
    async def test_set_default_ttl(self, session_store, redis_client):
        """Test session storage with default TTL"""
        await session_store.set("test-123", {"session_id": "test-123"})

        ttl = await redis_client.ttl("session:test-123")
        assert ttl > 0
        assert ttl <= session_store.default_ttl

    @pytest.mark.asyncio
    async def test_delete_success(self, session_store, redis_client):
        """Test successful session deletion"""
        await redis_client.set("session:test-123", '{"data":"test"}', ex=60)

        result = await session_store.delete("test-123")

        assert result is True
        assert await redis_client.exists("session:test-123") == 0

    @pytest.mark.asyncio
    async def test_delete_not_found(self, session_store):
        """Test deletion of non-existent session"""
        result = await session_store.delete("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_exists_true(self, session_store, redis_client):
        """Test session existence check for existing session"""
        await redis_client.set("session:test-123", '{"data":"test"}', ex=60)

        result = await session_store.exists("test-123")
        assert result is True

    @pytest.mark.asyncio
    async def test_exists_false(self, session_store):
        """Test session existence check for non-existent session"""
        result = await session_store.exists("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_extend_ttl_success(self, session_store, redis_client):
        """Test successful TTL extension"""
        await redis_client.set("session:test-123", '{"data":"test"}', ex=60)

        result = await session_store.extend_ttl("test-123", ttl=7200)

        assert result is True
        ttl = await redis_client.ttl("session:test-123")
        assert ttl > 60  # Extended beyond original
        assert ttl <= 7200

    @pytest.mark.asyncio
    async def test_extend_ttl_default(self, session_store, redis_client):
        """Test TTL extension with default TTL"""
        await redis_client.set("session:test-123", '{"data":"test"}', ex=60)

        result = await session_store.extend_ttl("test-123")

        assert result is True
        ttl = await redis_client.ttl("session:test-123")
        assert ttl <= session_store.default_ttl

    @pytest.mark.asyncio
    async def test_extend_ttl_failure(self, session_store):
        """Test TTL extension for non-existent session"""
        result = await session_store.extend_ttl("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_prefix_consistency(self, session_store, redis_client):
        """Test that prefix is consistently applied across all operations"""
        test_key = "test-session"
        expected_full_key = f"session:{test_key}"

        # set → get roundtrip
        await session_store.set(test_key, {"test": "data"})
        assert await redis_client.exists(expected_full_key) == 1

        result = await session_store.get(test_key)
        assert result is not None
        assert result["test"] == "data"

        # exists
        assert await session_store.exists(test_key) is True

        # extend_ttl
        await session_store.extend_ttl(test_key)

        # delete
        await session_store.delete(test_key)
        assert await redis_client.exists(expected_full_key) == 0

    def test_configuration_defaults(self, redis_client):
        """Test default configuration values"""
        store = RedisSessionStore(redis_client)

        assert store.default_ttl == 1800
        assert store.prefix == "session:"

    @pytest.mark.asyncio
    async def test_json_serialization_deserialization(self, session_store):
        """Test proper JSON handling for complex session data"""
        complex_session_data = {
            "session_id": "test-123",
            "user_id": "user-456",
            "data_uploads": ["file1.log", "file2.log"],
            "case_history": [
                {"timestamp": "2025-01-01T12:00:00", "action": "upload"},
                {"timestamp": "2025-01-01T12:05:00", "action": "analyze"},
            ],
            "nested_data": {"key1": "value1", "key2": ["item1", "item2"]},
        }

        await session_store.set("test-123", complex_session_data)
        retrieved_data = await session_store.get("test-123")

        assert retrieved_data["session_id"] == "test-123"
        assert retrieved_data["data_uploads"] == ["file1.log", "file2.log"]
        assert len(retrieved_data["case_history"]) == 2
        assert retrieved_data["nested_data"]["key2"] == ["item1", "item2"]

    @pytest.mark.asyncio
    async def test_edge_cases(self, session_store, redis_client):
        """Test edge cases and boundary conditions"""
        # Empty session data — last_activity still added
        await session_store.set("empty", {})
        result = await session_store.get("empty")
        assert "last_activity" in result

        # Very long session key
        long_key = "x" * 1000
        await session_store.set(long_key, {"key": "value"})
        result = await session_store.get(long_key)
        assert result["key"] == "value"

        # TTL of 1 (minimum valid TTL — Redis rejects 0)
        await session_store.set("short-ttl", {"data": "test"}, ttl=1)
        result = await session_store.get("short-ttl")
        assert result["data"] == "test"


@pytest.mark.integration
class TestRedisSessionStoreIntegration:
    """Integration tests for RedisSessionStore"""

    @pytest.fixture
    def redis_client(self):
        import fakeredis.aioredis as fakeredis_aio

        return fakeredis_aio.FakeRedis(decode_responses=True)

    @pytest.fixture
    def session_store(self, redis_client):
        return RedisSessionStore(redis_client)

    @pytest.mark.asyncio
    async def test_interface_compliance_comprehensive(self, redis_client):
        """Comprehensive test of interface compliance"""
        store = RedisSessionStore(redis_client)

        assert hasattr(store, "get")
        assert hasattr(store, "set")
        assert hasattr(store, "delete")
        assert hasattr(store, "exists")
        assert hasattr(store, "extend_ttl")

        get_sig = inspect.signature(store.get)
        assert "key" in get_sig.parameters

        set_sig = inspect.signature(store.set)
        assert "key" in set_sig.parameters
        assert "value" in set_sig.parameters
        assert "ttl" in set_sig.parameters
        assert set_sig.parameters["ttl"].default is None

        delete_sig = inspect.signature(store.delete)
        assert "key" in delete_sig.parameters

        exists_sig = inspect.signature(store.exists)
        assert "key" in exists_sig.parameters

        extend_ttl_sig = inspect.signature(store.extend_ttl)
        assert "key" in extend_ttl_sig.parameters
        assert "ttl" in extend_ttl_sig.parameters
        assert extend_ttl_sig.parameters["ttl"].default is None

    @pytest.mark.asyncio
    async def test_session_lifecycle(self, session_store):
        """Test complete session lifecycle"""
        session_data = {
            "session_id": "lifecycle-test",
            "user_id": "test-user",
            "created_at": "2025-01-01T10:00:00",
        }

        # Create
        await session_store.set("lifecycle-test", session_data, ttl=3600)

        # Retrieve
        retrieved = await session_store.get("lifecycle-test")
        assert retrieved["session_id"] == "lifecycle-test"

        # Exists
        assert await session_store.exists("lifecycle-test") is True

        # Extend TTL
        assert await session_store.extend_ttl("lifecycle-test", ttl=7200) is True

        # Delete
        assert await session_store.delete("lifecycle-test") is True

        # Gone
        assert await session_store.exists("lifecycle-test") is False
