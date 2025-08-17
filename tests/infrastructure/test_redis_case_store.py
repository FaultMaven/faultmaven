"""Test module for Redis case store implementation.

This module tests the RedisCaseStore class which implements ICaseStore interface,
focusing on Redis data persistence, serialization, and storage layer functionality.

Tests cover:
- Case and message storage/retrieval
- Data serialization/deserialization
- Participant management
- Search and filtering functionality
- Redis operations and error handling
- Cleanup and maintenance operations
"""

import json
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch
from typing import List, Dict, Any

from faultmaven.infrastructure.persistence.redis_case_store import RedisCaseStore
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
from faultmaven.exceptions import ServiceException


class MockRedisClient:
    """Mock Redis client for testing"""
    
    def __init__(self):
        self.data = {}
        self.sets = {}
        self.lists = {}
        self.ttls = {}
        
        # Mock methods
        self.hset = AsyncMock(return_value=True)
        self.hget = AsyncMock(return_value=None)
        self.hgetall = AsyncMock(return_value={})
        self.expire = AsyncMock(return_value=True)
        self.delete = AsyncMock(return_value=True)
        self.exists = AsyncMock(return_value=False)
        self.ttl = AsyncMock(return_value=-1)
        self.sadd = AsyncMock(return_value=1)
        self.srem = AsyncMock(return_value=1)
        self.smembers = AsyncMock(return_value=set())
        self.lpush = AsyncMock(return_value=1)
        self.lrange = AsyncMock(return_value=[])
        self.pipeline = Mock()
        self.aclose = AsyncMock()
        self.close = AsyncMock()
        
        # Setup pipeline mock
        pipeline_mock = Mock()
        pipeline_mock.hset = Mock(return_value=pipeline_mock)
        pipeline_mock.expire = Mock(return_value=pipeline_mock)
        pipeline_mock.lpush = Mock(return_value=pipeline_mock)
        pipeline_mock.sadd = Mock(return_value=pipeline_mock)
        pipeline_mock.srem = Mock(return_value=pipeline_mock)
        pipeline_mock.delete = Mock(return_value=pipeline_mock)
        pipeline_mock.execute = AsyncMock(return_value=[True, True, True, True])
        self.pipeline.return_value = pipeline_mock
        self.pipeline_instance = pipeline_mock


@pytest.fixture
def mock_redis_client():
    """Fixture providing mock Redis client"""
    return MockRedisClient()


@pytest.fixture
def redis_case_store(mock_redis_client):
    """Fixture providing RedisCaseStore with mock Redis client"""
    return RedisCaseStore(redis_client=mock_redis_client)


@pytest.fixture
def sample_case():
    """Fixture providing sample case for testing"""
    case = Case(
        case_id="case-123",
        title="Sample Redis Case",
        description="Test case for Redis storage",
        owner_id="user-456",
        status=CaseStatus.ACTIVE,
        priority=CasePriority.MEDIUM,
        tags=["redis", "test"]
    )
    case.add_participant("user-456", ParticipantRole.OWNER)
    return case


@pytest.fixture
def sample_message():
    """Fixture providing sample case message"""
    return CaseMessage(
        message_id="msg-123",
        case_id="case-123",
        session_id="session-789",
        author_id="user-456",
        message_type=MessageType.USER_QUERY,
        content="Test message for Redis storage",
        metadata={"source": "test"}
    )


class TestRedisCaseStoreInitialization:
    """Test RedisCaseStore initialization and configuration"""
    
    def test_redis_case_store_init_with_client(self, mock_redis_client):
        """Test RedisCaseStore initialization with provided Redis client"""
        store = RedisCaseStore(redis_client=mock_redis_client)
        
        assert store.redis_client == mock_redis_client
        assert store.case_key_pattern == "case:{case_id}"
        assert store.case_messages_key_pattern == "case:{case_id}:messages"
        assert store.user_cases_key_pattern == "user:{user_id}:cases"
        assert store.case_index_key == "cases:index"
        assert store.default_case_ttl == 30 * 24 * 3600
        assert store.message_batch_size == 100
        assert store.search_result_limit == 1000
    
    @patch('faultmaven.infrastructure.persistence.redis_case_store.create_redis_client')
    def test_redis_case_store_init_default_client(self, mock_create_client):
        """Test RedisCaseStore initialization with default Redis client"""
        mock_client = Mock()
        mock_create_client.return_value = mock_client
        
        store = RedisCaseStore()
        
        assert store.redis_client == mock_client
        mock_create_client.assert_called_once()


class TestCaseSerialization:
    """Test case serialization and deserialization"""
    
    @pytest.mark.asyncio
    async def test_serialize_case_basic(self, redis_case_store, sample_case):
        """Test basic case serialization"""
        case_data, messages = await redis_case_store._serialize_case(sample_case)
        
        assert isinstance(case_data, dict)
        assert isinstance(messages, list)
        assert case_data["case_id"] == "case-123"
        assert case_data["title"] == "Sample Redis Case"
        assert case_data["status"] == "active"
        assert case_data["priority"] == "medium"
        assert "redis" in case_data["tags"]
        
        # Check datetime serialization
        assert case_data["created_at"].endswith("Z")
        assert case_data["updated_at"].endswith("Z")
        
        # Check set serialization
        assert isinstance(case_data["session_ids"], list)
        
        # Check participants serialization
        assert len(case_data["participants"]) == 1
        assert case_data["participants"][0]["user_id"] == "user-456"
        assert case_data["participants"][0]["role"] == "owner"
    
    @pytest.mark.asyncio
    async def test_serialize_case_with_messages(self, redis_case_store, sample_case, sample_message):
        """Test case serialization with messages"""
        sample_case.add_message(sample_message)
        
        case_data, messages = await redis_case_store._serialize_case(sample_case)
        
        assert case_data["message_count"] == 1
        assert len(messages) == 1
        assert "messages" not in case_data  # Messages stored separately
    
    @pytest.mark.asyncio
    async def test_serialize_case_error_handling(self, redis_case_store):
        """Test case serialization error handling"""
        # Create invalid case that can't be serialized
        invalid_case = Mock()
        invalid_case.dict.side_effect = Exception("Serialization error")
        
        with pytest.raises(ServiceException, match="Case serialization failed"):
            await redis_case_store._serialize_case(invalid_case)
    
    @pytest.mark.asyncio
    async def test_deserialize_case_basic(self, redis_case_store):
        """Test basic case deserialization"""
        case_data = {
            "case_id": "case-123",
            "title": "Test Case",
            "status": "active",
            "priority": "medium",
            "created_at": "2024-01-01T12:00:00Z",
            "updated_at": "2024-01-01T12:00:00Z",
            "last_activity_at": "2024-01-01T12:00:00Z",
            "session_ids": ["session-1", "session-2"],
            "participants": [{
                "user_id": "user-123",
                "role": "owner",
                "added_at": "2024-01-01T12:00:00Z",
                "last_accessed": None,
                "can_edit": True,
                "can_share": True,
                "can_archive": True
            }],
            "tags": ["test"],
            "metadata": {},
            "context": {}
        }
        
        case = await redis_case_store._deserialize_case(case_data)
        
        assert isinstance(case, Case)
        assert case.case_id == "case-123"
        assert case.title == "Test Case"
        assert case.status == CaseStatus.ACTIVE
        assert case.priority == CasePriority.MEDIUM
        assert isinstance(case.created_at, datetime)
        assert isinstance(case.session_ids, set)
        assert "session-1" in case.session_ids
        assert len(case.participants) == 1
        assert case.participants[0].user_id == "user-123"
        assert case.participants[0].role == ParticipantRole.OWNER
    
    @pytest.mark.asyncio
    async def test_deserialize_case_with_messages(self, redis_case_store):
        """Test case deserialization with messages"""
        case_data = {
            "case_id": "case-123",
            "title": "Test Case",
            "status": "active",
            "priority": "medium",
            "created_at": "2024-01-01T12:00:00Z",
            "updated_at": "2024-01-01T12:00:00Z",
            "last_activity_at": "2024-01-01T12:00:00Z",
            "session_ids": [],
            "participants": [],
            "tags": [],
            "metadata": {},
            "context": {}
        }
        
        messages = [{
            "message_id": "msg-1",
            "case_id": "case-123",
            "message_type": "user_query",
            "content": "Test message",
            "timestamp": "2024-01-01T12:00:00Z",
            "metadata": {},
            "attachments": []
        }]
        
        case = await redis_case_store._deserialize_case(case_data, messages)
        
        assert len(case.messages) == 1
        assert case.messages[0].message_id == "msg-1"
        assert case.messages[0].message_type == MessageType.USER_QUERY
        assert isinstance(case.messages[0].timestamp, datetime)
    
    @pytest.mark.asyncio
    async def test_deserialize_case_error_handling(self, redis_case_store):
        """Test case deserialization error handling"""
        invalid_data = {"invalid": "data"}
        
        with pytest.raises(ServiceException, match="Case deserialization failed"):
            await redis_case_store._deserialize_case(invalid_data)


class TestCaseStorage:
    """Test case storage operations"""
    
    @pytest.mark.asyncio
    async def test_create_case_success(self, redis_case_store, mock_redis_client, sample_case):
        """Test successful case creation"""
        mock_redis_client.pipeline_instance.execute.return_value = [True, True, True, True]
        
        result = await redis_case_store.create_case(sample_case)
        
        assert result is True
        
        # Verify pipeline operations
        pipeline = mock_redis_client.pipeline_instance
        pipeline.hset.assert_called()
        pipeline.expire.assert_called()
        pipeline.sadd.assert_called()
        pipeline.execute.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_create_case_with_messages(self, redis_case_store, mock_redis_client, sample_case, sample_message):
        """Test case creation with messages"""
        sample_case.add_message(sample_message)
        mock_redis_client.pipeline_instance.execute.return_value = [True, True, True, True]
        
        result = await redis_case_store.create_case(sample_case)
        
        assert result is True
        
        # Verify message storage
        pipeline = mock_redis_client.pipeline_instance
        pipeline.lpush.assert_called()
    
    @pytest.mark.asyncio
    async def test_create_case_pipeline_failure(self, redis_case_store, mock_redis_client, sample_case):
        """Test case creation with pipeline failure"""
        mock_redis_client.pipeline_instance.execute.return_value = [False, True, True, True]
        
        result = await redis_case_store.create_case(sample_case)
        
        assert result is False
    
    @pytest.mark.asyncio
    async def test_create_case_exception(self, redis_case_store, mock_redis_client, sample_case):
        """Test case creation with exception"""
        mock_redis_client.pipeline_instance.execute.side_effect = Exception("Redis error")
        
        result = await redis_case_store.create_case(sample_case)
        
        assert result is False
    
    @pytest.mark.asyncio
    async def test_get_case_success(self, redis_case_store, mock_redis_client):
        """Test successful case retrieval"""
        case_data = {
            "data": json.dumps({
                "case_id": "case-123",
                "title": "Test Case",
                "status": "active",
                "priority": "medium",
                "created_at": "2024-01-01T12:00:00Z",
                "updated_at": "2024-01-01T12:00:00Z",
                "last_activity_at": "2024-01-01T12:00:00Z",
                "session_ids": [],
                "participants": [],
                "tags": [],
                "metadata": {},
                "context": {}
            })
        }
        
        messages_data = [
            json.dumps({
                "message_id": "msg-1",
                "case_id": "case-123",
                "message_type": "user_query",
                "content": "Test message",
                "timestamp": "2024-01-01T12:00:00Z",
                "metadata": {},
                "attachments": []
            })
        ]
        
        mock_redis_client.hgetall.return_value = case_data
        mock_redis_client.lrange.return_value = messages_data
        
        case = await redis_case_store.get_case("case-123")
        
        assert case is not None
        assert case.case_id == "case-123"
        assert case.title == "Test Case"
        assert len(case.messages) == 1
        assert case.messages[0].content == "Test message"
    
    @pytest.mark.asyncio
    async def test_get_case_not_found(self, redis_case_store, mock_redis_client):
        """Test case retrieval when case doesn't exist"""
        mock_redis_client.hgetall.return_value = {}
        
        case = await redis_case_store.get_case("case-999")
        
        assert case is None
    
    @pytest.mark.asyncio
    async def test_get_case_no_data_field(self, redis_case_store, mock_redis_client):
        """Test case retrieval when data field is missing"""
        mock_redis_client.hgetall.return_value = {"other_field": "value"}
        
        case = await redis_case_store.get_case("case-123")
        
        assert case is None
    
    @pytest.mark.asyncio
    async def test_get_case_exception(self, redis_case_store, mock_redis_client):
        """Test case retrieval with exception"""
        mock_redis_client.hgetall.side_effect = Exception("Redis error")
        
        case = await redis_case_store.get_case("case-123")
        
        assert case is None
    
    @pytest.mark.asyncio
    async def test_update_case_success(self, redis_case_store, mock_redis_client):
        """Test successful case update"""
        existing_data = {
            "case_id": "case-123",
            "title": "Old Title",
            "status": "active"
        }
        
        mock_redis_client.hget.return_value = json.dumps(existing_data)
        mock_redis_client.pipeline_instance.execute.return_value = [True, True]
        
        updates = {
            "title": "New Title",
            "description": "Updated description"
        }
        
        result = await redis_case_store.update_case("case-123", updates)
        
        assert result is True
        
        # Verify pipeline operations
        pipeline = mock_redis_client.pipeline_instance
        pipeline.hset.assert_called()
        pipeline.execute.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_update_case_not_found(self, redis_case_store, mock_redis_client):
        """Test case update when case doesn't exist"""
        mock_redis_client.hget.return_value = None
        
        result = await redis_case_store.update_case("case-999", {"title": "New Title"})
        
        assert result is False
    
    @pytest.mark.asyncio
    async def test_delete_case_success(self, redis_case_store, mock_redis_client):
        """Test successful case deletion"""
        case_data = {
            "owner_id": "user-123",
            "status": "active",
            "data": json.dumps({"priority": "medium"})
        }
        
        mock_redis_client.hgetall.return_value = case_data
        mock_redis_client.pipeline_instance.execute.return_value = [True, True, True]
        
        result = await redis_case_store.delete_case("case-123")
        
        assert result is True
        
        # Verify cleanup operations
        pipeline = mock_redis_client.pipeline_instance
        pipeline.delete.assert_called()
        pipeline.srem.assert_called()
    
    @pytest.mark.asyncio
    async def test_delete_case_failure(self, redis_case_store, mock_redis_client):
        """Test case deletion failure"""
        mock_redis_client.hgetall.return_value = {}
        mock_redis_client.pipeline_instance.execute.return_value = [False, True, True]
        
        result = await redis_case_store.delete_case("case-123")
        
        assert result is False


class TestCaseMessages:
    """Test case message operations"""
    
    @pytest.mark.asyncio
    async def test_add_message_success(self, redis_case_store, mock_redis_client, sample_message):
        """Test successful message addition"""
        case_data = {"message_count": 5}
        mock_redis_client.hget.return_value = json.dumps(case_data)
        mock_redis_client.ttl.return_value = 3600
        mock_redis_client.pipeline_instance.execute.return_value = [True, True]
        
        result = await redis_case_store.add_message_to_case("case-123", sample_message)
        
        assert result is True
        
        # Verify operations
        pipeline = mock_redis_client.pipeline_instance
        pipeline.lpush.assert_called()
        pipeline.hset.assert_called()
        pipeline.expire.assert_called()
    
    @pytest.mark.asyncio
    async def test_add_message_no_case_data(self, redis_case_store, mock_redis_client, sample_message):
        """Test message addition when case data doesn't exist"""
        mock_redis_client.hget.return_value = None
        mock_redis_client.pipeline_instance.execute.return_value = [True]
        
        result = await redis_case_store.add_message_to_case("case-123", sample_message)
        
        assert result is True
        
        # Should still add message even without case data
        pipeline = mock_redis_client.pipeline_instance
        pipeline.lpush.assert_called()
    
    @pytest.mark.asyncio
    async def test_get_case_messages_success(self, redis_case_store, mock_redis_client):
        """Test successful message retrieval"""
        messages_data = [
            json.dumps({
                "message_id": "msg-1",
                "case_id": "case-123",
                "message_type": "user_query",
                "content": "Message 1",
                "timestamp": "2024-01-01T12:00:00Z",
                "metadata": {},
                "attachments": []
            }),
            json.dumps({
                "message_id": "msg-2",
                "case_id": "case-123",
                "message_type": "agent_response",
                "content": "Message 2",
                "timestamp": "2024-01-01T12:01:00Z",
                "metadata": {},
                "attachments": []
            })
        ]
        
        mock_redis_client.lrange.return_value = messages_data
        
        messages = await redis_case_store.get_case_messages("case-123", limit=10)
        
        assert len(messages) == 2
        assert messages[0].content == "Message 1"  # Chronological order
        assert messages[1].content == "Message 2"
        assert messages[0].message_type == MessageType.USER_QUERY
        assert messages[1].message_type == MessageType.AGENT_RESPONSE
    
    @pytest.mark.asyncio
    async def test_get_case_messages_with_pagination(self, redis_case_store, mock_redis_client):
        """Test message retrieval with pagination"""
        mock_redis_client.lrange.return_value = []
        
        await redis_case_store.get_case_messages("case-123", limit=5, offset=10)
        
        mock_redis_client.lrange.assert_called_with(
            "case:case-123:messages", 10, 14
        )
    
    @pytest.mark.asyncio
    async def test_get_case_messages_parse_error(self, redis_case_store, mock_redis_client):
        """Test message retrieval with parse errors"""
        messages_data = [
            json.dumps({"valid": "message", "message_id": "msg-1", "case_id": "case-123", 
                       "message_type": "user_query", "content": "Valid", 
                       "timestamp": "2024-01-01T12:00:00Z", "metadata": {}, "attachments": []}),
            "invalid json",
            json.dumps({"another": "valid", "message_id": "msg-2", "case_id": "case-123",
                       "message_type": "agent_response", "content": "Valid 2",
                       "timestamp": "2024-01-01T12:01:00Z", "metadata": {}, "attachments": []})
        ]
        
        mock_redis_client.lrange.return_value = messages_data
        
        messages = await redis_case_store.get_case_messages("case-123")
        
        # Should only get valid messages
        assert len(messages) == 2
        assert messages[0].content == "Valid"
        assert messages[1].content == "Valid 2"


class TestParticipantManagement:
    """Test participant management operations"""
    
    @pytest.mark.asyncio
    async def test_add_participant_success(self, redis_case_store, mock_redis_client):
        """Test successful participant addition"""
        case_data = {
            "participants": [
                {
                    "user_id": "user-1",
                    "role": "owner",
                    "added_at": "2024-01-01T12:00:00Z",
                    "can_edit": True,
                    "can_share": True,
                    "can_archive": True
                }
            ],
            "participant_count": 1
        }
        
        mock_redis_client.hget.return_value = json.dumps(case_data)
        mock_redis_client.ttl.return_value = 3600
        
        result = await redis_case_store.add_case_participant(
            "case-123", "user-2", ParticipantRole.COLLABORATOR, "user-1"
        )
        
        assert result is True
        
        # Verify operations
        mock_redis_client.hset.assert_called()
        mock_redis_client.sadd.assert_called()
        mock_redis_client.expire.assert_called()
    
    @pytest.mark.asyncio
    async def test_add_participant_duplicate(self, redis_case_store, mock_redis_client):
        """Test adding duplicate participant"""
        case_data = {
            "participants": [
                {
                    "user_id": "user-1",
                    "role": "owner"
                }
            ]
        }
        
        mock_redis_client.hget.return_value = json.dumps(case_data)
        
        result = await redis_case_store.add_case_participant(
            "case-123", "user-1", ParticipantRole.COLLABORATOR
        )
        
        assert result is False
    
    @pytest.mark.asyncio
    async def test_add_participant_case_not_found(self, redis_case_store, mock_redis_client):
        """Test adding participant to non-existent case"""
        mock_redis_client.hget.return_value = None
        
        result = await redis_case_store.add_case_participant(
            "case-999", "user-1", ParticipantRole.COLLABORATOR
        )
        
        assert result is False
    
    @pytest.mark.asyncio
    async def test_remove_participant_success(self, redis_case_store, mock_redis_client):
        """Test successful participant removal"""
        case_data = {
            "participants": [
                {"user_id": "user-1", "role": "owner"},
                {"user_id": "user-2", "role": "collaborator"}
            ],
            "participant_count": 2
        }
        
        mock_redis_client.hget.return_value = json.dumps(case_data)
        mock_redis_client.pipeline_instance.execute.return_value = [True, True]
        
        result = await redis_case_store.remove_case_participant("case-123", "user-2")
        
        assert result is True
        
        # Verify operations
        pipeline = mock_redis_client.pipeline_instance
        pipeline.hset.assert_called()
        pipeline.srem.assert_called()
    
    @pytest.mark.asyncio
    async def test_remove_participant_owner_protection(self, redis_case_store, mock_redis_client):
        """Test that owner cannot be removed"""
        case_data = {
            "participants": [
                {"user_id": "user-1", "role": "owner"}
            ]
        }
        
        mock_redis_client.hget.return_value = json.dumps(case_data)
        
        result = await redis_case_store.remove_case_participant("case-123", "user-1")
        
        assert result is False
    
    @pytest.mark.asyncio
    async def test_remove_participant_not_found(self, redis_case_store, mock_redis_client):
        """Test removing non-existent participant"""
        case_data = {
            "participants": [
                {"user_id": "user-1", "role": "owner"}
            ]
        }
        
        mock_redis_client.hget.return_value = json.dumps(case_data)
        
        result = await redis_case_store.remove_case_participant("case-123", "user-999")
        
        assert result is False


class TestCaseListing:
    """Test case listing and filtering operations"""
    
    @pytest.mark.asyncio
    async def test_list_cases_basic(self, redis_case_store, mock_redis_client):
        """Test basic case listing"""
        mock_redis_client.smembers.return_value = {"case-1", "case-2"}
        
        # Mock case data
        case_data_1 = {
            "data": json.dumps({
                "case_id": "case-1",
                "title": "Case 1",
                "status": "active",
                "priority": "medium",
                "owner_id": "user-1",
                "created_at": "2024-01-01T12:00:00Z",
                "updated_at": "2024-01-01T12:00:00Z",
                "last_activity_at": "2024-01-01T12:00:00Z",
                "message_count": 5,
                "participants": [],
                "tags": []
            })
        }
        
        case_data_2 = {
            "data": json.dumps({
                "case_id": "case-2",
                "title": "Case 2",
                "status": "solved",
                "priority": "high",
                "owner_id": "user-2",
                "created_at": "2024-01-01T13:00:00Z",
                "updated_at": "2024-01-01T13:00:00Z",
                "last_activity_at": "2024-01-01T13:00:00Z",
                "message_count": 10,
                "participants": [],
                "tags": ["urgent"]
            })
        }
        
        # Mock Redis responses for each case
        def mock_hgetall(key):
            if "case-1" in key:
                return case_data_1
            elif "case-2" in key:
                return case_data_2
            return {}
        
        mock_redis_client.hgetall.side_effect = mock_hgetall
        
        cases = await redis_case_store.list_cases()
        
        assert len(cases) == 2
        assert cases[0].case_id in ["case-1", "case-2"]
        assert cases[1].case_id in ["case-1", "case-2"]
    
    @pytest.mark.asyncio
    async def test_list_cases_with_status_filter(self, redis_case_store, mock_redis_client):
        """Test case listing with status filter"""
        filters = CaseListFilter(status=CaseStatus.ACTIVE)
        mock_redis_client.smembers.return_value = {"case-1", "case-2"}
        
        await redis_case_store.list_cases(filters)
        
        # Should query status-specific Redis set
        mock_redis_client.smembers.assert_called_with("cases:status:active")
    
    @pytest.mark.asyncio
    async def test_list_cases_with_user_filter(self, redis_case_store, mock_redis_client):
        """Test case listing with user filter"""
        filters = CaseListFilter(user_id="user-123")
        mock_redis_client.smembers.return_value = {"case-1"}
        
        await redis_case_store.list_cases(filters)
        
        # Should query user-specific Redis set
        mock_redis_client.smembers.assert_called_with("user:user-123:cases")
    
    @pytest.mark.asyncio
    async def test_list_cases_with_pagination(self, redis_case_store, mock_redis_client):
        """Test case listing with pagination"""
        filters = CaseListFilter(limit=2, offset=1)
        mock_redis_client.smembers.return_value = {"case-1", "case-2", "case-3"}
        
        # Mock empty case data to avoid full processing
        mock_redis_client.hgetall.return_value = {}
        
        await redis_case_store.list_cases(filters)
        
        # Should limit and offset results (implementation detail)
        assert mock_redis_client.hgetall.call_count <= 2
    
    @pytest.mark.asyncio
    async def test_list_cases_parse_error_handling(self, redis_case_store, mock_redis_client):
        """Test case listing with parse errors"""
        mock_redis_client.smembers.return_value = {"case-1", "case-2"}
        
        def mock_hgetall(key):
            if "case-1" in key:
                return {"data": "invalid json"}
            elif "case-2" in key:
                return {
                    "data": json.dumps({
                        "case_id": "case-2",
                        "title": "Valid Case",
                        "status": "active",
                        "priority": "medium",
                        "created_at": "2024-01-01T12:00:00Z",
                        "updated_at": "2024-01-01T12:00:00Z",
                        "last_activity_at": "2024-01-01T12:00:00Z",
                        "message_count": 0,
                        "participants": [],
                        "tags": []
                    })
                }
            return {}
        
        mock_redis_client.hgetall.side_effect = mock_hgetall
        
        cases = await redis_case_store.list_cases()
        
        # Should only return valid cases
        assert len(cases) == 1
        assert cases[0].case_id == "case-2"


class TestCaseSearch:
    """Test case search functionality"""
    
    @pytest.mark.asyncio
    async def test_search_cases_title_match(self, redis_case_store, mock_redis_client):
        """Test case search by title"""
        search_request = CaseSearchRequest(query="database")
        
        # Mock list_cases to return cases
        redis_case_store.list_cases = AsyncMock(return_value=[
            CaseSummary(
                case_id="case-1",
                title="Database Connection Issue",
                status=CaseStatus.ACTIVE,
                priority=CasePriority.HIGH,
                owner_id="user-1",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                last_activity_at=datetime.utcnow(),
                message_count=5,
                participant_count=1,
                tags=[]
            ),
            CaseSummary(
                case_id="case-2",
                title="Network Timeout Problem",
                status=CaseStatus.SOLVED,
                priority=CasePriority.MEDIUM,
                owner_id="user-2",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                last_activity_at=datetime.utcnow(),
                message_count=3,
                participant_count=1,
                tags=[]
            )
        ])
        
        results = await redis_case_store.search_cases(search_request)
        
        assert len(results) == 1
        assert results[0].case_id == "case-1"
        assert "Database" in results[0].title
    
    @pytest.mark.asyncio
    async def test_search_cases_message_content(self, redis_case_store, mock_redis_client):
        """Test case search in message content"""
        search_request = CaseSearchRequest(query="timeout", search_in_messages=True)
        
        # Mock list_cases to return cases
        redis_case_store.list_cases = AsyncMock(return_value=[
            CaseSummary(
                case_id="case-1",
                title="Connection Issue",
                status=CaseStatus.ACTIVE,
                priority=CasePriority.HIGH,
                owner_id="user-1",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                last_activity_at=datetime.utcnow(),
                message_count=5,
                participant_count=1,
                tags=[]
            )
        ])
        
        # Mock get_case_messages to return messages with search term
        redis_case_store.get_case_messages = AsyncMock(return_value=[
            CaseMessage(
                case_id="case-1",
                message_type=MessageType.USER_QUERY,
                content="The database connection has timeout errors",
                timestamp=datetime.utcnow()
            )
        ])
        
        results = await redis_case_store.search_cases(search_request)
        
        assert len(results) == 1
        assert results[0].case_id == "case-1"
    
    @pytest.mark.asyncio
    async def test_search_cases_no_matches(self, redis_case_store, mock_redis_client):
        """Test case search with no matches"""
        search_request = CaseSearchRequest(query="nonexistent")
        
        redis_case_store.list_cases = AsyncMock(return_value=[
            CaseSummary(
                case_id="case-1",
                title="Database Issue",
                status=CaseStatus.ACTIVE,
                priority=CasePriority.HIGH,
                owner_id="user-1",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                last_activity_at=datetime.utcnow(),
                message_count=5,
                participant_count=1,
                tags=[]
            )
        ])
        
        results = await redis_case_store.search_cases(search_request)
        
        assert len(results) == 0


class TestCaseActivity:
    """Test case activity tracking"""
    
    @pytest.mark.asyncio
    async def test_update_case_activity_success(self, redis_case_store, mock_redis_client):
        """Test successful case activity update"""
        case_data = {
            "case_id": "case-123",
            "session_ids": ["session-1"]
        }
        
        mock_redis_client.hget.return_value = json.dumps(case_data)
        
        result = await redis_case_store.update_case_activity("case-123", "session-2")
        
        assert result is True
        mock_redis_client.hset.assert_called()
    
    @pytest.mark.asyncio
    async def test_update_case_activity_case_not_found(self, redis_case_store, mock_redis_client):
        """Test activity update for non-existent case"""
        mock_redis_client.hget.return_value = None
        
        result = await redis_case_store.update_case_activity("case-999")
        
        assert result is False


class TestAnalyticsAndCleanup:
    """Test analytics and cleanup operations"""
    
    @pytest.mark.asyncio
    async def test_get_case_analytics_success(self, redis_case_store, mock_redis_client):
        """Test successful case analytics retrieval"""
        # Mock get_case to return a case
        case = Case(
            case_id="case-123",
            title="Analytics Test Case",
            created_at=datetime.utcnow() - timedelta(hours=5)
        )
        case.add_participant("user-1", ParticipantRole.OWNER)
        
        redis_case_store.get_case = AsyncMock(return_value=case)
        redis_case_store.get_case_messages = AsyncMock(return_value=[
            CaseMessage(
                case_id="case-123",
                message_type=MessageType.USER_QUERY,
                content="Query 1",
                timestamp=datetime.utcnow()
            ),
            CaseMessage(
                case_id="case-123",
                message_type=MessageType.AGENT_RESPONSE,
                content="Response 1",
                timestamp=datetime.utcnow()
            )
        ])
        
        analytics = await redis_case_store.get_case_analytics("case-123")
        
        assert analytics["case_id"] == "case-123"
        assert analytics["message_count"] == 2
        assert analytics["message_types"]["user_query"] == 1
        assert analytics["message_types"]["agent_response"] == 1
        assert analytics["participant_count"] == 1
        assert analytics["duration_hours"] > 0
        assert analytics["status"] == "active"
    
    @pytest.mark.asyncio
    async def test_get_case_analytics_case_not_found(self, redis_case_store, mock_redis_client):
        """Test analytics for non-existent case"""
        redis_case_store.get_case = AsyncMock(return_value=None)
        
        analytics = await redis_case_store.get_case_analytics("case-999")
        
        assert analytics == {}
    
    @pytest.mark.asyncio
    async def test_cleanup_expired_cases_success(self, redis_case_store, mock_redis_client):
        """Test successful expired case cleanup"""
        # Mock case index with expired and valid cases
        mock_redis_client.smembers.return_value = {"case-1", "case-2", "case-3"}
        
        def mock_exists(key):
            if "case-1" in key:
                return False  # Expired (TTL expired)
            return True
        
        def mock_hget(key, field):
            if "case-2" in key:
                # Manually expired case
                return json.dumps({
                    "expires_at": (datetime.utcnow() - timedelta(days=1)).isoformat() + "Z"
                })
            elif "case-3" in key:
                # Valid case
                return json.dumps({
                    "expires_at": (datetime.utcnow() + timedelta(days=1)).isoformat() + "Z"
                })
            return None
        
        mock_redis_client.exists.side_effect = mock_exists
        mock_redis_client.hget.side_effect = mock_hget
        
        # Mock delete_case
        redis_case_store.delete_case = AsyncMock(return_value=True)
        
        cleaned_count = await redis_case_store.cleanup_expired_cases()
        
        assert cleaned_count == 2  # case-1 (TTL) + case-2 (manual expiry)
        
        # Verify cleanup operations
        mock_redis_client.srem.assert_called_with("cases:index", "case-1")
        redis_case_store.delete_case.assert_called_with("case-2")
    
    @pytest.mark.asyncio
    async def test_cleanup_expired_cases_with_batching(self, redis_case_store, mock_redis_client):
        """Test expired case cleanup with batching"""
        # Create many cases to test batching
        case_ids = {f"case-{i}" for i in range(150)}
        mock_redis_client.smembers.return_value = case_ids
        
        # All cases exist (not TTL expired)
        mock_redis_client.exists.return_value = True
        
        # All cases are valid (not manually expired)
        mock_redis_client.hget.return_value = json.dumps({
            "expires_at": (datetime.utcnow() + timedelta(days=1)).isoformat() + "Z"
        })
        
        cleaned_count = await redis_case_store.cleanup_expired_cases(batch_size=50)
        
        assert cleaned_count == 0  # No cases should be cleaned
        
        # Verify batching (should process in chunks of 50)
        assert mock_redis_client.exists.call_count == 150


class TestErrorHandling:
    """Test error handling and edge cases"""
    
    @pytest.mark.asyncio
    async def test_close_connection_success(self, redis_case_store, mock_redis_client):
        """Test successful Redis connection closing"""
        await redis_case_store.close()
        
        mock_redis_client.aclose.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_close_connection_fallback(self, redis_case_store, mock_redis_client):
        """Test Redis connection closing with fallback method"""
        # Remove aclose method to test fallback
        del mock_redis_client.aclose
        
        await redis_case_store.close()
        
        mock_redis_client.close.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_close_connection_exception(self, redis_case_store, mock_redis_client):
        """Test Redis connection closing with exception"""
        mock_redis_client.aclose.side_effect = Exception("Close error")
        
        # Should not raise exception
        await redis_case_store.close()
    
    @pytest.mark.asyncio
    async def test_get_user_cases_success(self, redis_case_store, mock_redis_client):
        """Test successful user cases retrieval"""
        filters = CaseListFilter(status=CaseStatus.ACTIVE)
        expected_cases = []
        
        redis_case_store.list_cases = AsyncMock(return_value=expected_cases)
        
        result = await redis_case_store.get_user_cases("user-123", filters)
        
        assert result == expected_cases
        
        # Verify that user_id was set in filters
        redis_case_store.list_cases.assert_called_once()
        call_args = redis_case_store.list_cases.call_args[0][0]
        assert call_args.user_id == "user-123"
    
    @pytest.mark.asyncio
    async def test_get_user_cases_no_filters(self, redis_case_store, mock_redis_client):
        """Test user cases retrieval without filters"""
        redis_case_store.list_cases = AsyncMock(return_value=[])
        
        await redis_case_store.get_user_cases("user-123")
        
        # Should create default filter with user_id
        redis_case_store.list_cases.assert_called_once()
        call_args = redis_case_store.list_cases.call_args[0][0]
        assert call_args.user_id == "user-123"


@pytest.mark.parametrize("redis_error,expected_result", [
    (ConnectionError("Redis down"), False),
    (TimeoutError("Redis timeout"), False),
    (Exception("General error"), False),
])
@pytest.mark.asyncio
async def test_redis_error_handling(redis_case_store, mock_redis_client, sample_case, redis_error, expected_result):
    """Test Redis error handling for various error types"""
    mock_redis_client.pipeline_instance.execute.side_effect = redis_error
    
    result = await redis_case_store.create_case(sample_case)
    
    assert result == expected_result